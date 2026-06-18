"""The verify surface: per-node verification, the full audit, and the commit log.

``GET /verify/{cid}/`` verifies any node regardless of type. ``GET /audit/``
walks every stored node with a shared memoized verified-set (O(nodes + edges)),
applies the temporal-order check, and verifies the commit-log hash chain.
``GET /commits/`` exposes the observation log itself -- one entry per
submission, so occurrence counts survive content dedup.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import SessionDep
from ..ledger import check_temporal_order, verify_commit_log
from ..models import CommitLogEntry
from ..nodes import NODE_TABLES, get_row, verify_node
from .common import cid_from_hex

router = APIRouter(tags=["audit"])


class VerifyRead(BaseModel):
    cid: str
    valid: bool
    property_violated: str | None = None
    message: str | None = None


def _verify_response(session: Session, cid: bytes) -> VerifyRead:
    """One verdict per node: structural verify, then temporal order."""
    result = verify_node(session, cid)
    if result.is_ok:
        result = check_temporal_order(session, cid)
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
    commit_entries: int
    failures: list[AuditFailure]


class CommitEntryRead(BaseModel):
    seq: int
    cid: str
    recorded_at: datetime
    prev_hash: str
    entry_hash: str

    @classmethod
    def from_row(cls, row: CommitLogEntry) -> "CommitEntryRead":
        return cls(
            seq=row.seq,
            cid=row.cid.hex(),
            recorded_at=row.recorded_at,
            prev_hash=row.prev_hash.hex(),
            entry_hash=row.entry_hash.hex(),
        )


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
def full_audit(session: SessionDep) -> AuditRead:
    verified: set[bytes] = set()
    failures: list[AuditFailure] = []
    nodes_checked = 0
    for table in NODE_TABLES:
        for row in session.exec(select(table)).all():
            nodes_checked += 1
            result = verify_node(session, row.cid, verified=verified)
            if result.is_ok:
                result = check_temporal_order(session, row.cid)
            if result.is_err:
                failures.append(_failure(result))
    chain = verify_commit_log(session)
    if chain.is_err:
        failures.append(_failure(chain))
    entries = len(session.exec(select(CommitLogEntry)).all())
    return AuditRead(
        valid=not failures,
        nodes_checked=nodes_checked,
        commit_entries=entries,
        failures=failures,
    )


@router.get("/commits/", response_model=list[CommitEntryRead])
def commits(session: SessionDep, cid: str | None = None, limit: int = 100) -> list[CommitEntryRead]:
    query = select(CommitLogEntry).order_by(CommitLogEntry.seq)
    if cid is not None:
        query = query.where(CommitLogEntry.cid == cid_from_hex(cid))
    rows = session.exec(query.limit(limit)).all()
    return [CommitEntryRead.from_row(r) for r in rows]
