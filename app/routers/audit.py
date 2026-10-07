"""The verify surface: per-node verification, the full audit, and the commit log.

``GET /verify/{cid}/`` verifies any node regardless of type. ``GET /audit/``
walks every stored node with a shared memoized verified-set (O(nodes + edges)),
applies the temporal-order check (a node with no commit-log observation is an
``unlogged_node`` finding), reports commit-log entries whose node is gone as
``missing_node``, and verifies the commit-log hash chain.
Every full-audit read uses one committed database snapshot.
``GET /commits/`` exposes the observation log itself -- one entry per
submission, so occurrence counts survive content dedup. ``GET /ledger/head``
and ``after_seq`` paging let an external verifier keep its own anchors.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from ..db import AuditSessionDep, SessionDep
from ..ledger import check_coverage, check_temporal_order, verify_commit_log
from ..models import CommitLogEntry
from ..nodes import NODE_TABLES, get_row, verify_node
from .common import cid_from_hex

router = APIRouter(tags=["audit"])


class VerifyRead(BaseModel):
    cid: str
    valid: bool
    property_violated: str | None = None
    message: str | None = None


def _check(session: Session, cid: bytes, verified: set[bytes] | None = None):
    """One verdict per node: structural verify, then temporal order."""
    result = verify_node(session, cid, verified=verified)
    if result.is_ok:
        result = check_temporal_order(session, cid)
    return result


def _verify_response(session: Session, cid: bytes) -> VerifyRead:
    """One verdict per node: structural verify, then temporal order."""
    result = _check(session, cid)
    if result.is_ok:
        return VerifyRead(cid=cid.hex(), valid=True)
    return VerifyRead(
        cid=cid.hex(),
        valid=False,
        property_violated=result.property_violated,
        message=result.message,
    )


class AuditFailure(BaseModel):
    record_kind: str
    record_key: str
    property_violated: str
    message: str


class AuditRead(BaseModel):
    valid: bool
    nodes_checked: int
    nodes_verified: int
    commit_entries: int
    failures: list[AuditFailure]


class CommitEntryRead(BaseModel):
    seq: int
    cid: str
    recorded_at: datetime
    prev_hash: str
    entry_hash: str
    principal: str

    @classmethod
    def from_row(cls, row: CommitLogEntry) -> "CommitEntryRead":
        return cls(
            seq=row.seq,
            cid=row.cid.hex(),
            recorded_at=row.recorded_at,
            prev_hash=row.prev_hash.hex(),
            entry_hash=row.entry_hash.hex(),
            principal=row.principal,
        )


class LedgerHeadRead(BaseModel):
    ledger_id: str | None
    seq: int | None
    entry_hash: str | None


def _failure(result) -> AuditFailure:
    return AuditFailure(
        record_kind=result.record_kind,
        record_key=str(result.record_key),
        property_violated=result.property_violated,
        message=result.message,
    )


@router.get("/verify/{cid_hex}/", response_model=VerifyRead)
def verify(cid_hex: str, session: SessionDep) -> VerifyRead:
    cid = cid_from_hex(cid_hex)
    if get_row(session, cid) is None:
        raise HTTPException(status_code=404, detail=f"No node found at CID: {cid_hex}")
    return _verify_response(session, cid)


@router.get("/audit/", response_model=AuditRead)
def full_audit(session: AuditSessionDep) -> AuditRead:
    verified: set[bytes] = set()
    failures: list[AuditFailure] = []
    stored_cids: set[bytes] = set()
    nodes_checked = 0
    nodes_verified = 0
    for table in NODE_TABLES:
        for row in session.exec(select(table)).all():
            nodes_checked += 1
            stored_cids.add(row.cid)
            result = _check(session, row.cid, verified)
            if result.is_err:
                failures.append(_failure(result))
            else:
                nodes_verified += 1
    failures.extend(_failure(result) for result in check_coverage(session, stored_cids))
    chain = verify_commit_log(session)
    if chain.is_err:
        failures.append(_failure(chain))
    count = func.count()  # pylint: disable=not-callable
    entries = session.exec(select(count).select_from(CommitLogEntry)).one()
    return AuditRead(
        valid=not failures,
        nodes_checked=nodes_checked,
        nodes_verified=nodes_verified,
        commit_entries=entries,
        failures=failures,
    )


@router.get("/commits/", response_model=list[CommitEntryRead])
def commits(
    session: SessionDep,
    cid: str | None = None,
    after_seq: int | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[CommitEntryRead]:
    """Commit-log entries in ``seq`` order; page with ``after_seq`` set to the last ``seq`` seen."""
    query = select(CommitLogEntry).order_by(CommitLogEntry.seq)
    if cid is not None:
        query = query.where(CommitLogEntry.cid == cid_from_hex(cid))
    if after_seq is not None:
        query = query.where(CommitLogEntry.seq > after_seq)
    rows = session.exec(query.limit(limit)).all()
    return [CommitEntryRead.from_row(r) for r in rows]


@router.get("/ledger/head", response_model=LedgerHeadRead)
def head(session: SessionDep) -> LedgerHeadRead:
    """The latest commit-log entry, and the first entry's hash as the ledger's identity.

    Derived per request; all fields are null while the log is empty.
    """
    last = session.exec(select(CommitLogEntry).order_by(CommitLogEntry.seq.desc()).limit(1)).first()
    if last is None:
        return LedgerHeadRead(ledger_id=None, seq=None, entry_hash=None)
    first = session.exec(select(CommitLogEntry).order_by(CommitLogEntry.seq).limit(1)).first()
    return LedgerHeadRead(
        ledger_id=first.entry_hash.hex(), seq=last.seq, entry_hash=last.entry_hash.hex()
    )
