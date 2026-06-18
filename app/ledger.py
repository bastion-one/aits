"""The ledger write path and its derived read structures.

``record`` is the one way nodes enter the system: a single transaction that
computes the CID server-side, inserts the typed row (dedup on the CID primary
key), and appends a hash-chained ``CommitLogEntry`` with a server-assigned
``recorded_at``. Node inserts dedupe; the commit log always appends -- one
entry per submission, so observations stay countable even when content
collapses to one node.

The derived reads live here too: commit-chain verification, first-observation
lookup, temporal-order (monotonicity) checks, the per-agent activation head,
the session frontier, and recursive traceback.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlmodel import Session, select

from . import dag
from .hashing import ZERO_PREV
from .models import (
    Agent,
    CommitLogEntry,
    ConfigActivation,
    DataUniqueTag,
    LineageTag,
)
from .nodes import cids_from_hex, get_row, to_node
from .result import Err, Ok, VerificationResult


def _append_entry(session: Session, cid: bytes) -> CommitLogEntry:
    last = session.exec(select(CommitLogEntry).order_by(CommitLogEntry.seq.desc())).first()
    prev_hash = last.entry_hash if last is not None else ZERO_PREV
    recorded_at = datetime.now(timezone.utc)
    entry = CommitLogEntry(
        cid=cid,
        recorded_at=recorded_at,
        prev_hash=prev_hash,
        entry_hash=dag.commit_entry_hash(cid, recorded_at, prev_hash),
    )
    session.add(entry)
    return entry


def record(session: Session, row):
    """Commit one submission: node (dedup) + commit-log entry.

    Returns ``(stored_row, entry)`` where ``stored_row`` is the existing row
    when the content dedupes.
    """
    content, links = to_node(row)
    cid = dag.compute_cid(content, links)
    stored = session.get(type(row), cid)
    if stored is None:
        row.cid = cid
        session.add(row)
        stored = row
    entry = _append_entry(session, cid)
    session.commit()
    session.refresh(stored)
    session.refresh(entry)
    return stored, entry


def first_recorded_at(session: Session, cid: bytes) -> datetime | None:
    """The ledger's first observation of ``cid``, or ``None`` if never seen."""
    entry = session.exec(
        select(CommitLogEntry).where(CommitLogEntry.cid == cid).order_by(CommitLogEntry.seq)
    ).first()
    return entry.recorded_at if entry is not None else None


def verify_commit_log(session: Session) -> VerificationResult:
    """Verify the hash chain: each entry re-hashes and names its predecessor."""
    prev_hash = ZERO_PREV
    for entry in session.exec(select(CommitLogEntry).order_by(CommitLogEntry.seq)).all():
        if entry.prev_hash != prev_hash:
            return Err(
                record_kind="CommitLogEntry",
                record_key=entry.seq,
                property_violated="chain_broken",
                expected=prev_hash.hex(),
                actual=entry.prev_hash.hex(),
                message=f"commit-log entry {entry.seq} does not chain to its predecessor",
            )
        recomputed = dag.commit_entry_hash(entry.cid, entry.recorded_at, entry.prev_hash)
        if recomputed != entry.entry_hash:
            return Err(
                record_kind="CommitLogEntry",
                record_key=entry.seq,
                property_violated="entry_hash_mismatch",
                expected=entry.entry_hash.hex(),
                actual=recomputed.hex(),
                message=f"commit-log entry {entry.seq} no longer hashes to its stored entry_hash",
            )
        prev_hash = entry.entry_hash
    return Ok()


def check_temporal_order(session: Session, cid: bytes) -> VerificationResult:
    """Causal-time monotonicity: everything ``cid`` links must have been
    recorded no later than ``cid`` itself was first recorded.

    Structure alone accepts a fabricated merge that links a node observed
    *after* the claimed use; the commit log exposes the impossible timeline.
    """
    row = get_row(session, cid)
    if row is None:
        return Err(
            record_kind="node",
            record_key=cid.hex(),
            property_violated="dangling_link",
            expected=cid.hex(),
            actual=None,
            message=f"no node found at CID {cid.hex()}",
        )
    child_recorded = first_recorded_at(session, cid)
    if child_recorded is None:
        return Ok()
    _, links = to_node(row)
    for parent in dag.iter_link_cids(dag.normalize_links(links)):
        parent_recorded = first_recorded_at(session, parent)
        if parent_recorded is not None and parent_recorded > child_recorded:
            return Err(
                record_kind="node",
                record_key=cid.hex(),
                property_violated="temporal_order",
                expected=f"parent recorded <= {child_recorded.isoformat()}",
                actual=parent_recorded.isoformat(),
                message=(
                    f"link target {parent.hex()} was first recorded at "
                    f"{parent_recorded.isoformat()}, after this node's first observation "
                    f"at {child_recorded.isoformat()}"
                ),
            )
    return Ok()


def activation_rows(session: Session, agent_uuid: UUID) -> list[ConfigActivation]:
    """An agent's activation chain in genesis-to-head order."""
    rows = session.exec(
        select(ConfigActivation).where(ConfigActivation.agent_uuid == agent_uuid)
    ).all()
    by_prev = {row.prev_cid: row for row in rows}
    ordered: list[ConfigActivation] = []
    cursor = by_prev.get(None)
    while cursor is not None:
        ordered.append(cursor)
        cursor = by_prev.get(cursor.cid)
    if len(ordered) != len(rows):
        raise RuntimeError(f"activation chain for agent {agent_uuid} is not linear")
    return ordered


def activation_head(session: Session, agent_uuid: UUID) -> ConfigActivation | None:
    """The derived chain head: the activation no other activation names as
    ``prev``."""
    chain = activation_rows(session, agent_uuid)
    return chain[-1] if chain else None


def activate(session: Session, agent: Agent, config_cid: bytes) -> ConfigActivation:
    """Append an activation against the current (server-derived) chain head.

    Re-activating the config that is already active is a no-op returning the
    existing head -- the chain records changes, not re-saves.
    """
    head = activation_head(session, agent.uuid)
    if head is not None and head.config_cid == config_cid:
        return head
    row = ConfigActivation(
        agent_uuid=agent.uuid,
        agent_cid=agent.cid,
        config_cid=config_cid,
        prev_cid=head.cid if head is not None else None,
    )
    stored, _ = record(session, row)
    return stored


def session_frontier(rows: list[LineageTag]) -> set[bytes]:
    """Leaves of a session's DAG: nodes no intra-session child names in
    ``prev``. Derived from the session's rows, never stored."""
    referenced: set[bytes] = set()
    for row in rows:
        referenced |= cids_from_hex(row.prev_cids)
    return {row.cid for row in rows} - referenced


def traceback(session: Session, cid: bytes) -> list[DataUniqueTag]:
    """Every DUT contributing to ``cid`` through ``prev``/``derived_from``
    ancestry, ordered by ``occurred_at``.

    ``derived_from`` may target session watermark nodes (head-at-handoff, the
    default) or DUTs directly (the allowed special case); both contribute.
    """
    seen: set[bytes] = set()
    duts: dict[bytes, DataUniqueTag] = {}
    stack = [cid]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        lt = session.get(LineageTag, current)
        if lt is not None:
            stack.extend(cids_from_hex(lt.prev_cids) | cids_from_hex(lt.derived_from_cids))
            if lt.dut_cid is not None:
                stack.append(lt.dut_cid)
            continue
        dut = session.get(DataUniqueTag, current)
        if dut is not None:
            duts[current] = dut
    return sorted(duts.values(), key=lambda d: d.occurred_at)
