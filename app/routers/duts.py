"""Endpoints for ``DataUniqueTag`` span nodes.

A DUT is one inference round / tool call. Its content carries the text
roundtrip plus the self-asserted ``occurred_at`` and the runtime ``span_id``;
its named edges pin the agent genesis, the behavioral config, and the set of
byte artifacts it touched. The server computes the CID from the submitted
content; ``recorded_at`` lives in the commit log (``GET /commits/``), never
in the node. Verification is the generic ``GET /verify/{cid}/``.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..db import SessionDep
from ..ledger import record
from ..models import AgentConfig, Artifact, DataUniqueTag
from ..nodes import hex_cid_list
from .common import MAX_ITEMS, Label, LongText, cid_from_hex, ensure_aware, get_agent_row

router = APIRouter(tags=["duts"])


class DUTCreate(BaseModel):
    agent_uuid: UUID
    config_cid: str = Field(description="hex CID of the behavioral AgentConfig used")
    span_id: Label = Field(description="runtime span handle, assigned at span start")
    business_object_keys: list[Label] = Field(default_factory=list, max_length=MAX_ITEMS)
    sequence: int
    input_context: LongText
    agent_output: LongText
    occurred_at: datetime | None = Field(
        default=None, description="self-asserted event time; defaults to server now"
    )
    artifact_cids: list[str] = Field(
        default_factory=list,
        max_length=MAX_ITEMS,
        description="hex CIDs of the byte artifacts touched",
    )


class DUTRead(BaseModel):
    cid: str
    span_id: str
    business_object_keys: list[str]
    sequence: int
    input_context: str
    agent_output: str
    occurred_at: datetime
    agent_cid: str
    config_cid: str
    artifact_cids: list[str]

    @classmethod
    def from_row(cls, row: DataUniqueTag) -> "DUTRead":
        return cls(
            cid=row.cid.hex(),
            span_id=row.span_id,
            business_object_keys=row.business_object_keys,
            sequence=row.sequence,
            input_context=row.input_context,
            agent_output=row.agent_output,
            occurred_at=row.occurred_at,
            agent_cid=row.agent_cid.hex(),
            config_cid=row.config_cid.hex(),
            artifact_cids=row.artifact_cids,
        )


def build_dut_row(session, body: DUTCreate) -> DataUniqueTag:
    """Validate a DUT submission's references and assemble the (uncommitted)
    span row. Shared by ``POST /duts/`` and inline lineage node creation."""
    agent = get_agent_row(session, body.agent_uuid)
    config_cid = cid_from_hex(body.config_cid, field="config_cid")
    if session.get(AgentConfig, config_cid) is None:
        raise HTTPException(status_code=404, detail=f"AgentConfig not found: {body.config_cid}")
    artifact_cids: set[bytes] = set()
    for value in body.artifact_cids:
        cid = cid_from_hex(value, field="artifact_cids")
        if session.get(Artifact, cid) is None:
            raise HTTPException(status_code=404, detail=f"Artifact not found: {value}")
        artifact_cids.add(cid)
    return DataUniqueTag(
        span_id=body.span_id,
        business_object_keys=body.business_object_keys,
        sequence=body.sequence,
        input_context=body.input_context,
        agent_output=body.agent_output,
        occurred_at=ensure_aware(body.occurred_at, field="occurred_at"),
        agent_cid=agent.cid,
        config_cid=config_cid,
        artifact_cids=hex_cid_list(artifact_cids),
    )


@router.post("/duts/", response_model=DUTRead, status_code=status.HTTP_201_CREATED)
def create(body: DUTCreate, session: SessionDep) -> DUTRead:
    stored, _ = record(session, build_dut_row(session, body))
    return DUTRead.from_row(stored)


@router.get("/duts/{cid_hex}/", response_model=DUTRead)
def get(cid_hex: str, session: SessionDep) -> DUTRead:
    cid = cid_from_hex(cid_hex)
    row = session.get(DataUniqueTag, cid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"DUT not found: {cid_hex}")
    return DUTRead.from_row(row)
