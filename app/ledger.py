"""The ledger write path and its derived read structures.

``stage`` is the one way nodes enter a transaction: it computes the CID
server-side, inserts the typed row (dedup on the CID primary key), and
appends a hash-chained ``CommitLogEntry`` with a server-assigned
``recorded_at``, then flushes without committing. ``record`` wraps ``stage``
and commits, for callers that own a single-node write.

A Postgres advisory lock serializes the dedup lookup, the node insert, and
the commit-head append together, so concurrent identical submissions dedupe
to one node and still append one observation each.

Node inserts dedupe; the commit log always appends -- one entry per
submission, so observations stay countable even when content collapses to
one node.

The derived reads live here too: commit-chain verification, first-observation
lookup, temporal-order (monotonicity) checks, the per-agent activation head,
root derivation through ``prev``, the graph frontier, and recursive traceback.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, select

from . import dag
from .config import get_settings
from .hashing import ZERO_PREV, sha256_hash
from .models import (
    Agent,
    CommitLogEntry,
    ConfigActivation,
    DataUniqueTag,
    LedgerFormat,
    LineageTag,
)
from .nodes import cids_from_hex, get_row, to_node
from .result import Err, Ok, VerificationResult

# Postgres advisory-lock key for serializing commit-head selection. Chosen as
# a stable project constant; not derived from row content.
_COMMIT_HEAD_LOCK = 87204631


_LOCK_NOT_AVAILABLE = "55P03"
"""PostgreSQL SQLSTATE raised when ``lock_timeout`` expires."""


class CommitLockTimeout(Exception):
    """The commit lock was not acquired within ``commit_lock_timeout_ms``."""


class CidCollision(Exception):
    """A submission hashed to a stored CID whose node has different bytes."""

    def __init__(self, cid: bytes):
        self.cid = cid
        super().__init__(f"CID {cid.hex()} is already stored with different content")


class LineageGraphError(Exception):
    """Domain error while walking ``prev`` ancestry."""

    def __init__(self, cid: bytes):
        self.cid = cid
        super().__init__(cid.hex())


class MissingLineageTag(LineageGraphError):
    """No LT is stored at ``cid``."""


class ConflictingRoots(LineageGraphError):
    """``prev`` paths from ``cid`` resolve to more than one root."""

    def __init__(self, cid: bytes, roots: set[bytes]):
        self.roots = frozenset(roots)
        super().__init__(cid)


class CyclicLineage(LineageGraphError):
    """``prev`` ancestry of ``cid`` contains a cycle."""


def lock_commit_head(session: Session) -> None:
    """Serialize the dedup lookup, node insert, and commit-head append.

    Callers that read ledger state and then write based on it (such as the
    activation chain head) take the lock before the read, so the read and the
    write happen as one step. The lock is transaction-scoped and re-entrant,
    so the later ``stage()`` call takes it again harmlessly.

    An in-process lock cannot coordinate separate database sessions. Postgres
    takes a transaction-scoped advisory lock covering the whole ``stage()``
    call; SQLite writers are serialized by the database file lock (and unit
    tests share one connection).

    The wait is bounded by ``commit_lock_timeout_ms``; on expiry this raises
    :class:`CommitLockTimeout`, which the API reports as 503 with
    ``Retry-After``. The timeout is reset once the lock is held, so it does
    not apply to later lock waits in the same transaction.
    """
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    timeout_ms = int(get_settings().commit_lock_timeout_ms)
    session.execute(text(f"SET LOCAL lock_timeout = {timeout_ms}"))
    try:
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _COMMIT_HEAD_LOCK})
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE:
            raise CommitLockTimeout(f"commit lock not acquired within {timeout_ms} ms") from exc
        raise
    session.execute(text("SET LOCAL lock_timeout TO DEFAULT"))


# The ledger format this code writes and accepts. Format 1 specifies the
# canonical JSON encoder (RFC 8785) and the hash (SHA2-256) that make CIDs and
# entry hashes; the golden vectors pin the exact bytes.
FORMAT = {"format": 1, "hash": "sha2-256", "canonical": "rfc8785"}


def _read_format(session: Session) -> dict | None:
    row = session.get(LedgerFormat, 1)
    if row is None:
        return None
    return {"format": row.format, "hash": row.hash, "canonical": row.canonical}


def init_ledger(session: Session) -> None:
    """Write the ledger format record into a fresh database, or require it to match.

    A ledger with commit-log entries and no format record predates format 1
    and cannot be upgraded in place.
    """
    found = _read_format(session)
    if found is None:
        # pylint: disable-next=not-callable  # sqlalchemy func.* is generated at runtime
        entries = session.exec(select(func.count()).select_from(CommitLogEntry)).one()
        if entries:
            raise RuntimeError(
                f"database has {entries} commit-log entries and no ledger format record "
                "(pre-format-1 database); a fresh database is required"
            )
        session.add(LedgerFormat(id=1, **FORMAT))
        try:
            session.commit()
            return
        except IntegrityError:
            # Another starter wrote the row first; check what it wrote.
            session.rollback()
            found = _read_format(session)
    if found != FORMAT:
        raise RuntimeError(f"ledger format {found} is not {FORMAT}")


_ATTRIBUTION = "aits.attribution"


def attest(session: Session, principal: str) -> None:
    """Record the authenticated identity that writes through ``session``.

    Every commit-log entry the session appends carries this principal. The
    auth dependency attests each request's session; scripts and tests call
    this directly. Appending without it raises, so no write path can record
    an entry nobody vouched for.
    """
    session.info[_ATTRIBUTION] = principal


def _append_entry(session: Session, cid: bytes) -> CommitLogEntry:
    principal = session.info.get(_ATTRIBUTION)
    if principal is None:
        raise RuntimeError("commit-log append on an unattested session; call ledger.attest()")
    # LIMIT 1 matters: ``.first()`` alone fetches the whole log, and this runs
    # under the commit lock, so append cost would grow with the ledger.
    head = select(CommitLogEntry).order_by(CommitLogEntry.seq.desc()).limit(1)
    last = session.exec(head).first()
    prev_hash = last.entry_hash if last is not None else ZERO_PREV
    recorded_at = datetime.now(timezone.utc)
    entry = CommitLogEntry(
        cid=cid,
        recorded_at=recorded_at,
        prev_hash=prev_hash,
        principal=principal,
        entry_hash=dag.commit_entry_hash(cid, recorded_at, prev_hash, principal),
    )
    session.add(entry)
    return entry


def stage(session: Session, row):
    """Flush one submission into the open transaction: node (dedup) + log entry.

    Returns ``(stored_row, entry)`` where ``stored_row`` is the existing row
    when the content dedupes. A dedup hit is confirmed by comparing canonical
    bytes; a stored row of the same type with different bytes is a SHA-256
    collision and raises ``CidCollision``. A cross-type collision is not
    separately checked: ``type`` is inside the hashed content, so it would also
    be a full SHA-256 collision. Does not commit.
    """
    lock_commit_head(session)
    data = dag.encode_node(*to_node(row))
    cid = sha256_hash(data)
    stored = session.get(type(row), cid)
    if stored is not None and dag.encode_node(*to_node(stored)) != data:
        raise CidCollision(cid)
    if stored is None:
        row.cid = cid
        session.add(row)
        stored = row
    entry = _append_entry(session, cid)
    session.flush()
    return stored, entry


def record(session: Session, row):
    """Commit one submission: node (dedup) + commit-log entry.

    Returns ``(stored_row, entry)`` where ``stored_row`` is the existing row
    when the content dedupes.
    """
    stored, entry = stage(session, row)
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
        recomputed = dag.commit_entry_hash(
            entry.cid, entry.recorded_at, entry.prev_hash, entry.principal
        )
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


def check_coverage(session: Session, stored_cids: set[bytes]) -> list[Err]:
    """Every CID in the observation log must have a stored node.

    A stored node with no observation is reported per node by
    ``check_temporal_order``. The caller must scan nodes and read the log on the
    same snapshot session. Repeated observations of a deduplicated node do not
    violate this invariant.
    """
    logged_cids = set(session.exec(select(CommitLogEntry.cid)).all())
    return [
        Err(
            record_kind="CommitLogEntry",
            record_key=cid.hex(),
            property_violated="missing_node",
            expected=cid.hex(),
            actual=None,
            message=f"commit-log entry references {cid.hex()} but no node is stored",
        )
        for cid in sorted(logged_cids - stored_cids)
    ]


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
        return Err(
            record_kind="node",
            record_key=cid.hex(),
            property_violated="unlogged_node",
            expected=cid.hex(),
            actual=None,
            message=f"node {cid.hex()} has no commit-log observation",
        )
    _, links = to_node(row)
    for parent in dag.iter_link_cids(dag.normalize_links(links)):
        parent_recorded = first_recorded_at(session, parent)
        if parent_recorded is None:
            return Err(
                record_kind="node",
                record_key=cid.hex(),
                property_violated="unobserved_link",
                expected="a commit-log entry for the link target",
                actual=parent.hex(),
                message=f"link target {parent.hex()} has no commit-log observation",
            )
        if parent_recorded > child_recorded:
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
    existing head -- the chain records changes, not re-saves. The head is
    read under the commit lock, so concurrent activations cannot both chain
    onto the same head.
    """
    lock_commit_head(session)
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


def _load_lt(
    session: Session, cid: bytes, rows_by_cid: dict[bytes, LineageTag] | None
) -> LineageTag:
    row = rows_by_cid.get(cid) if rows_by_cid is not None else session.get(LineageTag, cid)
    if row is None:
        raise MissingLineageTag(cid)
    return row


def resolve_root(
    session: Session,
    lt_cid: bytes,
    *,
    memo: dict[bytes, bytes] | None = None,
    rows_by_cid: dict[bytes, LineageTag] | None = None,
) -> bytes:
    """Return the unique terminal root of ``lt_cid`` by walking only ``prev``.

    A root is an LT with no ``prev``. Shared ancestors are resolved once via
    ``memo``. Missing LTs, cycles, and ``prev`` paths that disagree on the
    root raise domain errors; they do not assign membership.
    """
    if memo is None:
        memo = {}
    if lt_cid in memo:
        return memo[lt_cid]

    visiting: set[bytes] = set()
    stack: list[tuple[bytes, bool]] = [(lt_cid, False)]

    while stack:
        cid, expanded = stack.pop()
        if cid in memo:
            continue
        if not expanded:
            if cid in visiting:
                raise CyclicLineage(cid)
            row = _load_lt(session, cid, rows_by_cid)
            prev = cids_from_hex(row.prev_cids)
            if not prev:
                memo[cid] = cid
                continue
            visiting.add(cid)
            stack.append((cid, True))
            for parent in prev:
                if parent not in memo:
                    stack.append((parent, False))
            continue

        visiting.discard(cid)
        row = _load_lt(session, cid, rows_by_cid)
        prev = cids_from_hex(row.prev_cids)
        roots: set[bytes] = set()
        for parent in prev:
            if parent not in memo:
                raise CyclicLineage(cid)
            roots.add(memo[parent])
        if len(roots) != 1:
            raise ConflictingRoots(cid, roots)
        memo[cid] = next(iter(roots))

    return memo[lt_cid]


def graphs_by_root(
    session: Session,
    *,
    rows: list[LineageTag] | None = None,
    memo: dict[bytes, bytes] | None = None,
) -> dict[bytes, list[LineageTag]]:
    """Group stored LTs by the root derived through ``prev``.

    Loads every LT when ``rows`` is omitted. ``derived_from`` does not affect
    membership. Shared ``memo`` avoids repeating walks across the scan.
    """
    if rows is None:
        rows = list(session.exec(select(LineageTag)).all())
    by_cid = {row.cid: row for row in rows}
    if memo is None:
        memo = {}
    grouped: dict[bytes, list[LineageTag]] = {}
    for row in rows:
        root = resolve_root(session, row.cid, memo=memo, rows_by_cid=by_cid)
        grouped.setdefault(root, []).append(row)
    return grouped


def graph_frontier(rows: list[LineageTag]) -> set[bytes]:
    """Leaves of a graph: nodes no intra-graph child names in ``prev``.

    Derived from the selected graph's rows, never stored. ``derived_from``
    does not affect the frontier.
    """
    referenced: set[bytes] = set()
    for row in rows:
        referenced |= cids_from_hex(row.prev_cids)
    return {row.cid for row in rows} - referenced


def traceback(session: Session, cid: bytes) -> list[DataUniqueTag]:
    """Every DUT contributing to ``cid`` through ``prev``/``derived_from``
    ancestry, ordered by ``occurred_at``.

    Traceback describes provenance, not graph membership: it follows both
    ``prev`` and ``derived_from``. DUTs are collected from LT attachments.
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
        if lt is None:
            continue
        stack.extend(cids_from_hex(lt.prev_cids) | cids_from_hex(lt.derived_from_cids))
        if lt.dut_cid is not None:
            dut = session.get(DataUniqueTag, lt.dut_cid)
            if dut is not None:
                duts[lt.dut_cid] = dut
    return sorted(duts.values(), key=lambda d: d.occurred_at)
