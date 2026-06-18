"""Endpoints for behavioral ``AgentConfig`` value objects and the per-agent
``ConfigActivation`` chain.

Config content is behavioral-only -- no agent reference, no time -- so
identical configurations dedupe globally and the CID is the UAI. Which config
an agent *should* be running is the activation chain: append-only nodes
``{agent, config, prev}`` whose head is derived. ``PUT /agents/{uuid}/config/``
is the opinionated path (dedup + activate against the server-resolved head);
``POST /agents/{uuid}/activations/`` keeps the explicit compare-and-set guard
for concurrency-careful callers.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session

from ..db import SessionDep
from ..ledger import activate as activate_config
from ..ledger import activation_head, activation_rows, record
from ..models import AgentConfig, ConfigActivation
from .common import cid_from_hex, get_agent_row, optional_cid

router = APIRouter(tags=["configs"])


class ConfigCreate(BaseModel):
    system_prompt: str
    llm_config: dict[str, Any] = Field(default_factory=dict)
    tools: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConfigRead(BaseModel):
    cid: str
    system_prompt: str
    llm_config: dict[str, Any]
    tools: list[Any]
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: AgentConfig) -> "ConfigRead":
        return cls(
            cid=row.cid.hex(),
            system_prompt=row.system_prompt,
            llm_config=row.llm_config,
            tools=row.tools,
            metadata=row.config_metadata,
        )


class ActivationCreate(BaseModel):
    config_cid: str
    expected_head: str | None = Field(
        default=None,
        description=(
            "CID (hex) of the activation the client believes is the current head; "
            "null for the first activation. Stale heads are rejected (409)."
        ),
    )


class ActivationRead(BaseModel):
    cid: str
    agent_uuid: UUID
    config_cid: str
    prev_cid: str | None

    @classmethod
    def from_row(cls, row: ConfigActivation) -> "ActivationRead":
        return cls(
            cid=row.cid.hex(),
            agent_uuid=row.agent_uuid,
            config_cid=row.config_cid.hex(),
            prev_cid=row.prev_cid.hex() if row.prev_cid is not None else None,
        )


def _get_config_row(session: Session, cid: bytes, *, hex_value: str) -> AgentConfig:
    row = session.get(AgentConfig, cid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"AgentConfig not found: {hex_value}")
    return row


def config_row(body: ConfigCreate) -> AgentConfig:
    return AgentConfig(
        system_prompt=body.system_prompt,
        llm_config=body.llm_config,
        tools=body.tools,
        config_metadata=body.metadata,
    )


@router.post("/configs/", response_model=ConfigRead, status_code=status.HTTP_201_CREATED)
def create(body: ConfigCreate, session: SessionDep) -> ConfigRead:
    stored, _ = record(session, config_row(body))
    return ConfigRead.from_row(stored)


@router.get("/configs/{cid_hex}/", response_model=ConfigRead)
def get(cid_hex: str, session: SessionDep) -> ConfigRead:
    cid = cid_from_hex(cid_hex)
    return ConfigRead.from_row(_get_config_row(session, cid, hex_value=cid_hex))


@router.post(
    "/agents/{agent_uuid}/activations/",
    response_model=ActivationRead,
    status_code=status.HTTP_201_CREATED,
)
def activate(agent_uuid: UUID, body: ActivationCreate, session: SessionDep) -> ActivationRead:
    agent = get_agent_row(session, agent_uuid)
    config_cid = cid_from_hex(body.config_cid, field="config_cid")
    _get_config_row(session, config_cid, hex_value=body.config_cid)
    expected = optional_cid(body.expected_head, field="expected_head")
    head = activation_head(session, agent_uuid)
    head_cid = head.cid if head is not None else None
    if expected != head_cid:
        raise HTTPException(
            status_code=409,
            detail=(
                "stale head: expected_head "
                f"{expected.hex() if expected else None} does not match the current head "
                f"{head_cid.hex() if head_cid else None}"
            ),
        )
    return ActivationRead.from_row(activate_config(session, agent, config_cid))


@router.put("/agents/{agent_uuid}/config/", response_model=ActivationRead)
def set_config(agent_uuid: UUID, body: ConfigCreate, session: SessionDep) -> ActivationRead:
    """Make this config the agent's active one, in one call: record the
    behavioral content (dedup) and activate it against the server-resolved
    chain head. Setting the already-active config is a no-op. Callers that
    need the compare-and-set guard use ``POST .../activations/`` instead."""
    agent = get_agent_row(session, agent_uuid)
    config, _ = record(session, config_row(body))
    return ActivationRead.from_row(activate_config(session, agent, config.cid))


@router.get("/agents/{agent_uuid}/activations/", response_model=list[ActivationRead])
def activation_history(agent_uuid: UUID, session: SessionDep) -> list[ActivationRead]:
    get_agent_row(session, agent_uuid)
    return [ActivationRead.from_row(r) for r in activation_rows(session, agent_uuid)]


@router.get("/agents/{agent_uuid}/active-config/", response_model=ConfigRead)
def active_config(agent_uuid: UUID, session: SessionDep) -> ConfigRead:
    get_agent_row(session, agent_uuid)
    head = activation_head(session, agent_uuid)
    if head is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_uuid} has no activated config")
    return ConfigRead.from_row(
        _get_config_row(session, head.config_cid, hex_value=head.config_cid.hex())
    )
