"""Endpoints for ``LineageTag`` session-DAG watermark nodes.

A session is itself a DAG: every round appends a node whose ``prev`` set
carries intra-session causality (fork on parallel tool calls, join on
synthesis), ``derived_from`` carries cross-session merges (another agent's
head at handoff, or a specific DUT), and ``dut`` links the span the round
produced. The session read derives the frontier (the session's leaves); full
ancestry is the transitive closure of ``prev`` + ``derived_from``.

``record`` is the opinionated front door -- one call per round of work: it
builds the DUT (defaulting the config to the agent's active one), chains the
watermark node onto the session frontier (creating the session genesis on the
first round), and turns ``used_sessions`` into head-at-handoff
``derived_from`` links. The primitive ``append`` stays for explicit-CID
control. Verification is the generic ``GET /verify/{cid}/``.
"""

from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from .. import ledger
from ..db import SessionDep
from ..models import DataUniqueTag, LineageTag
from ..nodes import hex_cid_list
from .common import cid_from_hex, ensure_aware, get_agent_row, optional_cid
from .duts import DUTCreate, DUTRead, build_dut_row

router = APIRouter(tags=["lineage"])


class SessionCreate(BaseModel):
    actor_id: str
    step_id: str
    transformation: str = "session-start"
    session_uuid: UUID | None = Field(
        default=None, description="stable session key; defaults to a new uuid4"
    )
    occurred_at: datetime | None = None


class AppendCreate(BaseModel):
    actor_id: str
    step_id: str
    transformation: str
    prev: list[str] = Field(
        min_length=1,
        description="hex CIDs of this node's intra-session causal predecessors",
    )
    derived_from: list[str] = Field(
        default_factory=list,
        description="hex CIDs of cross-session inputs: another session's head node(s) or DUTs",
    )
    dut: str | None = Field(default=None, description="hex CID of the DUT this round produced")
    occurred_at: datetime | None = None


class LineageRead(BaseModel):
    cid: str
    session_uuid: UUID
    actor_id: str
    step_id: str
    transformation: str
    occurred_at: datetime
    prev: list[str]
    derived_from: list[str]
    dut: str | None

    @classmethod
    def from_row(cls, row: LineageTag) -> "LineageRead":
        return cls(
            cid=row.cid.hex(),
            session_uuid=row.session_uuid,
            actor_id=row.actor_id,
            step_id=row.step_id,
            transformation=row.transformation,
            occurred_at=row.occurred_at,
            prev=row.prev_cids,
            derived_from=row.derived_from_cids,
            dut=row.dut_cid.hex() if row.dut_cid is not None else None,
        )


class RecordCreate(BaseModel):
    agent_uuid: UUID
    span_id: str = Field(description="runtime span handle, assigned at span start")
    input_context: str
    agent_output: str
    transformation: str
    actor_id: str
    step_id: str
    config_cid: str | None = Field(
        default=None, description="hex CID of the config used; defaults to the agent's active one"
    )
    business_object_keys: list[str] = Field(default_factory=list)
    sequence: int = 0
    artifact_cids: list[str] = Field(default_factory=list)
    occurred_at: datetime | None = None
    prev: list[str] | None = Field(
        default=None,
        description="hex CIDs of intra-session predecessors; defaults to the session frontier",
    )
    used_sessions: list[UUID] = Field(
        default_factory=list,
        description=(
            "sessions whose results this round consumed; each one's frontier "
            "is linked in derived_from (head-at-handoff)"
        ),
    )


class RecordRead(BaseModel):
    dut: DUTRead
    node: LineageRead


class SessionRead(BaseModel):
    session_uuid: UUID
    nodes: list[LineageRead]
    frontier: list[str]


class SessionSummary(BaseModel):
    session_uuid: UUID
    nodes: int
    started_at: datetime
    last_occurred_at: datetime
    frontier: list[str]


class WatermarkRead(BaseModel):
    cid: str
    watermark: str


class TracebackStep(BaseModel):
    occurred_at: datetime
    dut_cid: str
    agent_output: str
    agent_cid: str
    config_cid: str


def _get_lt_row(session: Session, cid: bytes, *, hex_value: str) -> LineageTag:
    row = session.get(LineageTag, cid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Lineage node not found: {hex_value}")
    return row


def _session_rows(session: Session, session_uuid: UUID) -> list[LineageTag]:
    return list(
        session.exec(select(LineageTag).where(LineageTag.session_uuid == session_uuid)).all()
    )


def _resolve_prev(session: Session, session_uuid: UUID, values: list[str]) -> set[bytes]:
    prev: set[bytes] = set()
    for value in values:
        cid = cid_from_hex(value, field="prev")
        parent = session.get(LineageTag, cid)
        if parent is None:
            raise HTTPException(status_code=404, detail=f"Lineage node not found: {value}")
        if parent.session_uuid != session_uuid:
            raise HTTPException(
                status_code=400,
                detail=f"prev {value} belongs to session {parent.session_uuid}, not {session_uuid}",
            )
        prev.add(cid)
    return prev


@router.get("/lineage/", response_model=list[SessionSummary])
def list_sessions(session: SessionDep, limit: int = 100) -> list[SessionSummary]:
    """Enumerate sessions, most recently active first. Includes sub-sessions
    (e.g. subagent work); their parents reach them via ``derived_from``."""
    by_session: dict[UUID, list[LineageTag]] = {}
    for row in session.exec(select(LineageTag)).all():
        by_session.setdefault(row.session_uuid, []).append(row)
    summaries = [
        SessionSummary(
            session_uuid=session_uuid,
            nodes=len(rows),
            started_at=min(r.occurred_at for r in rows),
            last_occurred_at=max(r.occurred_at for r in rows),
            frontier=hex_cid_list(ledger.session_frontier(rows)),
        )
        for session_uuid, rows in by_session.items()
    ]
    summaries.sort(key=lambda s: s.last_occurred_at, reverse=True)
    return summaries[:limit]


@router.post("/lineage/", response_model=LineageRead, status_code=status.HTTP_201_CREATED)
def start_session(body: SessionCreate, session: SessionDep) -> LineageRead:
    row = LineageTag(
        session_uuid=body.session_uuid or uuid4(),
        actor_id=body.actor_id,
        step_id=body.step_id,
        transformation=body.transformation,
        occurred_at=ensure_aware(body.occurred_at, field="occurred_at"),
    )
    stored, _ = ledger.record(session, row)
    return LineageRead.from_row(stored)


@router.post(
    "/lineage/{session_uuid}/append/",
    response_model=LineageRead,
    status_code=status.HTTP_201_CREATED,
)
def append(session_uuid: UUID, body: AppendCreate, session: SessionDep) -> LineageRead:
    prev = _resolve_prev(session, session_uuid, body.prev)

    derived: set[bytes] = set()
    for value in body.derived_from:
        cid = cid_from_hex(value, field="derived_from")
        lt = session.get(LineageTag, cid)
        if lt is not None:
            if lt.session_uuid == session_uuid:
                raise HTTPException(
                    status_code=400,
                    detail=f"derived_from {value} is intra-session; link it via prev",
                )
        elif session.get(DataUniqueTag, cid) is None:
            raise HTTPException(status_code=404, detail=f"derived_from target not found: {value}")
        derived.add(cid)

    dut_cid = optional_cid(body.dut, field="dut")
    if dut_cid is not None and session.get(DataUniqueTag, dut_cid) is None:
        raise HTTPException(status_code=404, detail=f"DUT not found: {body.dut}")

    row = LineageTag(
        session_uuid=session_uuid,
        actor_id=body.actor_id,
        step_id=body.step_id,
        transformation=body.transformation,
        occurred_at=ensure_aware(body.occurred_at, field="occurred_at"),
        dut_cid=dut_cid,
        prev_cids=hex_cid_list(prev),
        derived_from_cids=hex_cid_list(derived),
    )
    stored, _ = ledger.record(session, row)
    return LineageRead.from_row(stored)


def perform_record(session: Session, session_uuid: UUID, body: RecordCreate) -> RecordRead:
    """The shared ``record`` machinery: build the DUT span and its watermark
    node, chained onto the session, in one submission sequence. Used by the
    session-scoped and agent-scoped entry points."""
    get_agent_row(session, body.agent_uuid)
    config_cid = body.config_cid
    if config_cid is None:
        head = ledger.activation_head(session, body.agent_uuid)
        if head is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Agent {body.agent_uuid} has no active config; "
                    "pass config_cid or activate one first"
                ),
            )
        config_cid = head.config_cid.hex()

    derived: set[bytes] = set()
    for source in body.used_sessions:
        if source == session_uuid:
            raise HTTPException(
                status_code=400,
                detail=f"used_sessions includes the target session {source}; use prev instead",
            )
        source_rows = _session_rows(session, source)
        if not source_rows:
            raise HTTPException(status_code=404, detail=f"Session not found: {source}")
        derived |= ledger.session_frontier(source_rows)

    occurred_at = ensure_aware(body.occurred_at, field="occurred_at")
    if body.prev is not None:
        if not body.prev:
            raise HTTPException(status_code=400, detail="prev may not be an empty list")
        prev = _resolve_prev(session, session_uuid, body.prev)
    else:
        rows = _session_rows(session, session_uuid)
        if rows:
            prev = ledger.session_frontier(rows)
        else:
            genesis, _ = ledger.record(
                session,
                LineageTag(
                    session_uuid=session_uuid,
                    actor_id=body.actor_id,
                    step_id=body.step_id,
                    transformation="session-start",
                    occurred_at=occurred_at,
                ),
            )
            prev = {genesis.cid}

    dut_row = build_dut_row(
        session,
        DUTCreate(
            agent_uuid=body.agent_uuid,
            config_cid=config_cid,
            span_id=body.span_id,
            business_object_keys=body.business_object_keys,
            sequence=body.sequence,
            input_context=body.input_context,
            agent_output=body.agent_output,
            occurred_at=occurred_at,
            artifact_cids=body.artifact_cids,
        ),
    )
    dut, _ = ledger.record(session, dut_row)

    node, _ = ledger.record(
        session,
        LineageTag(
            session_uuid=session_uuid,
            actor_id=body.actor_id,
            step_id=body.step_id,
            transformation=body.transformation,
            occurred_at=occurred_at,
            dut_cid=dut.cid,
            prev_cids=hex_cid_list(prev),
            derived_from_cids=hex_cid_list(derived),
        ),
    )
    return RecordRead(dut=DUTRead.from_row(dut), node=LineageRead.from_row(node))


@router.post(
    "/lineage/{session_uuid}/record/",
    response_model=RecordRead,
    status_code=status.HTTP_201_CREATED,
)
def record(session_uuid: UUID, body: RecordCreate, session: SessionDep) -> RecordRead:
    """Record one round of work: the DUT span and its watermark node, chained
    onto the session, in a single call."""
    return perform_record(session, session_uuid, body)


@router.get("/lineage/nodes/{cid_hex}/", response_model=LineageRead)
def get_node(cid_hex: str, session: SessionDep) -> LineageRead:
    cid = cid_from_hex(cid_hex)
    return LineageRead.from_row(_get_lt_row(session, cid, hex_value=cid_hex))


@router.get("/lineage/nodes/{cid_hex}/watermark/", response_model=WatermarkRead)
def watermark(cid_hex: str, session: SessionDep) -> WatermarkRead:
    """Render the flat P1 watermark string straight off a node: its
    ``derived_from`` parents (each head's produced DUT when it has one), the
    transformation, and the asserted event time."""
    cid = cid_from_hex(cid_hex)
    row = _get_lt_row(session, cid, hex_value=cid_hex)
    parents = []
    for value in row.derived_from_cids:
        target = bytes.fromhex(value)
        lt = session.get(LineageTag, target)
        if lt is not None and lt.dut_cid is not None:
            target = lt.dut_cid
        parents.append(target.hex()[:8])
    ts = row.occurred_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    rendered = f"Parent:[{', '.join(sorted(parents))}]_Transformation:[{row.transformation}]_{ts}"
    return WatermarkRead(cid=cid_hex, watermark=rendered)


@router.get("/lineage/nodes/{cid_hex}/traceback/", response_model=list[TracebackStep])
def trace_back(cid_hex: str, session: SessionDep) -> list[TracebackStep]:
    """Every contributing DUT in the node's ancestry, ordered by event time,
    each carrying its agent + config attribution."""
    cid = cid_from_hex(cid_hex)
    _get_lt_row(session, cid, hex_value=cid_hex)
    return [
        TracebackStep(
            occurred_at=dut.occurred_at,
            dut_cid=dut.cid.hex(),
            agent_output=dut.agent_output,
            agent_cid=dut.agent_cid.hex(),
            config_cid=dut.config_cid.hex(),
        )
        for dut in ledger.traceback(session, cid)
    ]


@router.get("/lineage/{session_uuid}/", response_model=SessionRead)
def get_session_nodes(session_uuid: UUID, session: SessionDep) -> SessionRead:
    rows = list(
        session.exec(
            select(LineageTag)
            .where(LineageTag.session_uuid == session_uuid)
            .order_by(LineageTag.occurred_at)
        ).all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_uuid}")
    return SessionRead(
        session_uuid=session_uuid,
        nodes=[LineageRead.from_row(r) for r in rows],
        frontier=hex_cid_list(ledger.session_frontier(rows)),
    )
