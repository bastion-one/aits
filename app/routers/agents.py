"""Endpoints for ``Agent`` genesis nodes and the mutable name annotation.

The agent is the opinionated entry point for identity. ``register`` commits
the genesis node ``{type, uuid, created_at}`` and, when a config is supplied
inline, also records the (deduped) behavioral config and its first
activation -- an agent ready to run, in one call. Callers then create or
select a lineage root explicitly and write nodes under it. The genesis CID
anchors attribution (every DUT links it directly). ``name`` is a ref-layer
annotation: overwritten in place, never in any hash, no history.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from .. import ledger
from ..db import SessionDep
from ..models import Agent, DataUniqueTag, LineageTag
from .common import Label, get_agent_row
from .configs import ConfigCreate, config_row
from .lineage import LineageGraphSummary, graph_summary, sort_summaries

router = APIRouter(tags=["agents"])


class AgentCreate(BaseModel):
    name: Label
    config: ConfigCreate | None = Field(
        default=None,
        description="Optional starting config; recorded (dedup) and activated in the same call",
    )


class AgentUpdate(BaseModel):
    name: Label


class AgentRead(BaseModel):
    cid: str
    uuid: UUID
    name: str
    created_at: datetime
    active_config_cid: str | None


def _read(session: Session, row: Agent) -> AgentRead:
    head = ledger.activation_head(session, row.uuid)
    return AgentRead(
        cid=row.cid.hex(),
        uuid=row.uuid,
        name=row.name,
        created_at=row.created_at,
        active_config_cid=head.config_cid.hex() if head is not None else None,
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
def list_all(
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[AgentRead]:
    """Agents in registration order, one page at a time."""
    rows = session.exec(
        select(Agent).order_by(Agent.created_at, Agent.cid).offset(offset).limit(limit)
    ).all()
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


@router.get("/agents/{agent_uuid}/sessions/", response_model=list[LineageGraphSummary])
def list_sessions(
    agent_uuid: UUID, session: SessionDep, limit: int = 100
) -> list[LineageGraphSummary]:
    """Graphs this agent produced work in, most recently active first.

    An agent participates in a graph when one of its DUTs (``agent_cid`` ==
    the agent's genesis cid) is linked by a lineage node. Structural actor
    labels alone do not establish participation. Each summary covers ALL
    nodes in that graph -- identical semantics to ``GET /lineage/`` -- so
    frontier and timestamps stay coherent even when a graph mixes actors.
    """
    agent = get_agent_row(session, agent_uuid)
    attached = list(
        session.exec(
            select(LineageTag).where(
                LineageTag.dut_cid.in_(
                    select(DataUniqueTag.cid).where(DataUniqueTag.agent_cid == agent.cid)
                )
            )
        ).all()
    )
    if not attached:
        return []
    memo: dict[bytes, bytes] = {}
    participating: set[bytes] = set()
    for row in attached:
        participating.add(ledger.resolve_root(session, row.cid, memo=memo))
    grouped = ledger.graphs_by_root(session, memo=memo)
    summaries = [graph_summary(root, grouped[root]) for root in participating if root in grouped]
    return sort_summaries(summaries)[:limit]
