"""Node assembly and structural verification over the typed tables.

``to_node`` is the single canonical assembler: it maps a typed row to the
``(content, links)`` pair whose CID is the row's primary key. The write path
(``app.ledger``) uses it to compute the CID at submission; ``resolve`` uses it
to feed ``app.dag.verify``, so tamper is caught by recomputing a node's hash
and comparing it to the address it is stored under.
"""

from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from . import dag
from .models import (
    Agent,
    AgentConfig,
    Artifact,
    ConfigActivation,
    DataUniqueTag,
    LineageTag,
)
from .result import VerificationResult

Node = tuple[Any, Mapping[str, Any]]

NODE_TABLES = (Agent, AgentConfig, ConfigActivation, DataUniqueTag, LineageTag, Artifact)
"""Every table whose primary key is a node CID, in resolve order."""


def cids_from_hex(values: list[str]) -> frozenset[bytes]:
    """Decode a JSON-stored list of hex CIDs into a link target set."""
    return frozenset(bytes.fromhex(v) for v in values)


def hex_cid_list(cids: "set[bytes] | frozenset[bytes] | list[bytes]") -> list[str]:
    """Encode a link target set as a sorted JSON-storable list of hex CIDs."""
    return sorted(c.hex() for c in cids)


def _agent_node(row: Agent) -> Node:
    content = {"type": "Agent", "uuid": row.uuid, "created_at": row.created_at}
    return content, {}


def _config_node(row: AgentConfig) -> Node:
    content = {
        "type": "AgentConfig",
        "system_prompt": row.system_prompt,
        "llm_config": row.llm_config,
        "tools": row.tools,
        "metadata": row.config_metadata,
    }
    return content, {}


def _activation_node(row: ConfigActivation) -> Node:
    content = {"type": "ConfigActivation", "agent_uuid": row.agent_uuid}
    links = {"agent": row.agent_cid, "config": row.config_cid, "prev": row.prev_cid}
    return content, links


def _dut_node(row: DataUniqueTag) -> Node:
    content = {
        "type": "DUT",
        "span_id": row.span_id,
        "business_object_keys": row.business_object_keys,
        "sequence": row.sequence,
        "input_context": row.input_context,
        "agent_output": row.agent_output,
        "occurred_at": row.occurred_at,
    }
    links = {
        "agent": row.agent_cid,
        "config": row.config_cid,
        "artifacts": cids_from_hex(row.artifact_cids),
    }
    return content, links


def _lineage_node(row: LineageTag) -> Node:
    content = {
        "type": "LT",
        "session_uuid": row.session_uuid,
        "actor_id": row.actor_id,
        "step_id": row.step_id,
        "transformation": row.transformation,
        "occurred_at": row.occurred_at,
    }
    links = {
        "prev": cids_from_hex(row.prev_cids),
        "derived_from": cids_from_hex(row.derived_from_cids),
        "dut": row.dut_cid,
    }
    return content, links


def _artifact_node(row: Artifact) -> Node:
    content = {"type": "Artifact", "sha256": row.sha256.hex()}
    return content, {}


_ASSEMBLERS = {
    Agent: _agent_node,
    AgentConfig: _config_node,
    ConfigActivation: _activation_node,
    DataUniqueTag: _dut_node,
    LineageTag: _lineage_node,
    Artifact: _artifact_node,
}


def to_node(row: Any) -> Node:
    """Assemble a typed row into its canonical ``(content, links)`` pair."""
    assembler = _ASSEMBLERS.get(type(row))
    if assembler is None:
        raise TypeError(f"not a node row: {type(row).__name__}")
    return assembler(row)


def get_row(session: Session, cid: bytes) -> Any | None:
    """Return the typed row stored at ``cid``, whichever table holds it."""
    for table in NODE_TABLES:
        row = session.get(table, cid)
        if row is not None:
            return row
    return None


def resolve(session: Session, cid: bytes) -> Node | None:
    """Resolver over the typed tables, in the shape ``app.dag.verify`` needs."""
    row = get_row(session, cid)
    if row is None:
        return None
    return to_node(row)


def verify_node(
    session: Session, cid: bytes, verified: set[bytes] | None = None
) -> VerificationResult:
    """Structurally verify the node at ``cid`` and everything it links."""
    return dag.verify(cid, lambda c: resolve(session, c), verified=verified)
