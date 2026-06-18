"""Endpoints for ``Agent`` genesis nodes and the mutable annotations.

The agent is the opinionated entry point for the whole flow. ``register``
commits the genesis node ``{type, uuid, created_at}`` and, when a config is
supplied inline, also records the (deduped) behavioral config and its first
activation -- an agent ready to run, in one call. ``record`` then captures a
round of the agent's work without re-stating identity: it chains onto the
agent's **current session** (a mutable ref-layer pointer, auto-started on
first use, rotated via ``new_session``). The genesis CID anchors attribution
(every DUT links it directly). ``name`` and the session pointer are ref-layer
annotations: overwritten in place, never in any hash, no history.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from .. import ledger
from ..db import SessionDep
from ..models import Agent, DataUniqueTag, LineageTag
from ..nodes import hex_cid_list
from .common import get_agent_row
from .configs import ConfigCreate, config_row
from .lineage import LineageRead, RecordCreate, RecordRead, SessionCreate, SessionSummary
from .lineage import perform_record
from .lineage import start_session as lineage_start_session

router = APIRouter(tags=["agents"])


class AgentCreate(BaseModel):
    name: str
    config: ConfigCreate | None = Field(
        default=None,
        description="Optional starting config; recorded (dedup) and activated in the same call",
    )


class AgentUpdate(BaseModel):
    name: str


class AgentRead(BaseModel):
    cid: str
    uuid: UUID
    name: str
    created_at: datetime
    active_config_cid: str | None
    current_session_uuid: UUID | None


class AgentRecordCreate(BaseModel):
    """One round of this agent's work; identity and session come from the path
    and the agent's state. ``actor_id`` defaults to the agent's name,
    ``step_id`` to the ``span_id``."""

    span_id: str = Field(description="runtime span handle, assigned at span start")
    input_context: str
    agent_output: str
    transformation: str
    actor_id: str | None = None
    step_id: str | None = None
    config_cid: str | None = Field(
        default=None, description="hex CID of the config used; defaults to the agent's active one"
    )
    business_object_keys: list[str] = Field(default_factory=list)
    sequence: int = 0
    artifact_cids: list[str] = Field(default_factory=list)
    occurred_at: datetime | None = None
    prev: list[str] | None = None
    used_sessions: list[UUID] = Field(default_factory=list)


class NewSessionCreate(BaseModel):
    session_uuid: UUID | None = Field(
        default=None, description="stable session key; defaults to a new uuid4"
    )
    step_id: str = "session"


def _read(session: Session, row: Agent) -> AgentRead:
    head = ledger.activation_head(session, row.uuid)
    return AgentRead(
        cid=row.cid.hex(),
        uuid=row.uuid,
        name=row.name,
        created_at=row.created_at,
        active_config_cid=head.config_cid.hex() if head is not None else None,
        current_session_uuid=row.current_session_uuid,
    )


@router.post("/agents/", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
def register(body: AgentCreate, session: SessionDep) -> AgentRead:
    config_cid: bytes | None = None
    if body.config is not None:
        config, _ = ledger.record(session, config_row(body.config))
        config_cid = config.cid
    agent, _ = ledger.record(
        session, Agent(uuid=uuid4(), created_at=datetime.now(timezone.utc), name=body.name)
    )
    if config_cid is not None:
        ledger.activate(session, agent, config_cid)
    return _read(session, agent)


@router.get("/agents/", response_model=list[AgentRead])
def list_all(session: SessionDep) -> list[AgentRead]:
    rows = session.exec(select(Agent)).all()
    return [_read(session, r) for r in rows]


@router.get("/agents/{agent_uuid}/", response_model=AgentRead)
def get(agent_uuid: UUID, session: SessionDep) -> AgentRead:
    return _read(session, get_agent_row(session, agent_uuid))


@router.patch("/agents/{agent_uuid}/", response_model=AgentRead)
def rename(agent_uuid: UUID, body: AgentUpdate, session: SessionDep) -> AgentRead:
    row = get_agent_row(session, agent_uuid)
    row.name = body.name
    session.add(row)
    session.commit()
    session.refresh(row)
    return _read(session, row)


@router.post(
    "/agents/{agent_uuid}/record/",
    response_model=RecordRead,
    status_code=status.HTTP_201_CREATED,
)
def record(agent_uuid: UUID, body: AgentRecordCreate, session: SessionDep) -> RecordRead:
    """Record one round of this agent's work into its current session,
    starting a session if it has none. Concurrent operations on one agent
    should use ``POST /lineage/{session_uuid}/record/`` with explicit sessions
    instead of sharing this pointer."""
    agent = get_agent_row(session, agent_uuid)
    target_session = agent.current_session_uuid or uuid4()
    result = perform_record(
        session,
        target_session,
        RecordCreate(
            agent_uuid=agent_uuid,
            span_id=body.span_id,
            input_context=body.input_context,
            agent_output=body.agent_output,
            transformation=body.transformation,
            actor_id=body.actor_id or agent.name,
            step_id=body.step_id or body.span_id,
            config_cid=body.config_cid,
            business_object_keys=body.business_object_keys,
            sequence=body.sequence,
            artifact_cids=body.artifact_cids,
            occurred_at=body.occurred_at,
            prev=body.prev,
            used_sessions=body.used_sessions,
        ),
    )
    if agent.current_session_uuid is None:
        agent.current_session_uuid = target_session
        session.add(agent)
        session.commit()
    return result


@router.get("/agents/{agent_uuid}/sessions/", response_model=list[SessionSummary])
def list_sessions(agent_uuid: UUID, session: SessionDep, limit: int = 100) -> list[SessionSummary]:
    """Sessions this agent produced work in, most recently active first.

    An agent participates in a session when one of its DUTs (``agent_cid`` ==
    the agent's genesis cid) is linked by a lineage node. Each summary covers
    ALL nodes in that session -- identical semantics to ``GET /lineage/`` -- so
    frontier and timestamps stay coherent even when a session mixes actors.
    """
    agent = get_agent_row(session, agent_uuid)
    session_uuids = set(
        session.exec(
            select(LineageTag.session_uuid).where(
                LineageTag.dut_cid.in_(
                    select(DataUniqueTag.cid).where(DataUniqueTag.agent_cid == agent.cid)
                )
            )
        ).all()
    )
    if not session_uuids:
        return []
    by_session: dict[UUID, list[LineageTag]] = {}
    for row in session.exec(
        select(LineageTag).where(LineageTag.session_uuid.in_(session_uuids))
    ).all():
        by_session.setdefault(row.session_uuid, []).append(row)
    summaries = [
        SessionSummary(
            session_uuid=sid,
            nodes=len(rows),
            started_at=min(r.occurred_at for r in rows),
            last_occurred_at=max(r.occurred_at for r in rows),
            frontier=hex_cid_list(ledger.session_frontier(rows)),
        )
        for sid, rows in by_session.items()
    ]
    summaries.sort(key=lambda s: s.last_occurred_at, reverse=True)
    return summaries[:limit]


@router.post(
    "/agents/{agent_uuid}/sessions/",
    response_model=LineageRead,
    status_code=status.HTTP_201_CREATED,
)
def new_session(agent_uuid: UUID, body: NewSessionCreate, session: SessionDep) -> LineageRead:
    """Start a fresh session for this agent and point its current-session
    ref at it; subsequent ``record`` calls chain there. Returns the session's
    genesis node."""
    agent = get_agent_row(session, agent_uuid)
    agent.current_session_uuid = body.session_uuid or uuid4()
    session.add(agent)
    return lineage_start_session(
        SessionCreate(
            actor_id=agent.name,
            step_id=body.step_id,
            session_uuid=agent.current_session_uuid,
        ),
        session,
    )
