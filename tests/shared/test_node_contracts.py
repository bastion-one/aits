"""Independent content/link contracts, field sensitivity, and mutable annotations."""

from datetime import timedelta
from uuid import UUID

import pytest

from app import dag, nodes
from app.models import Agent, AgentConfig, Artifact, ConfigActivation, DataUniqueTag, LineageTag
from tests.shared.test_golden_vectors import AGENT_UUID, AT

A, B, C = (bytes([n]) * 32 for n in (1, 2, 3))

CONTRACTS = [
    (
        Agent(uuid=AGENT_UUID, created_at=AT, name="worker"),
        {"v": 1, "type": "Agent", "uuid": AGENT_UUID, "created_at": AT},
        {},
        {
            "uuid": UUID("00000000-0000-4000-8000-000000000002"),
            "created_at": AT + timedelta(seconds=1),
        },
    ),
    (
        AgentConfig(
            system_prompt="p", llm_config={"model": "m"}, tools=["t"], config_metadata={"k": "v"}
        ),
        {
            "v": 1,
            "type": "AgentConfig",
            "system_prompt": "p",
            "llm_config": {"model": "m"},
            "tools": ["t"],
            "metadata": {"k": "v"},
        },
        {},
        {
            "system_prompt": "q",
            "llm_config": {"model": "n"},
            "tools": ["u"],
            "config_metadata": {"k": "w"},
        },
    ),
    (
        ConfigActivation(agent_uuid=AGENT_UUID, agent_cid=A, config_cid=B, prev_cid=C),
        {"v": 1, "type": "ConfigActivation", "agent_uuid": AGENT_UUID},
        {"agent": A, "config": B, "prev": C},
        {
            "agent_uuid": UUID("00000000-0000-4000-8000-000000000002"),
            "agent_cid": B,
            "config_cid": C,
            "prev_cid": A,
        },
    ),
    (
        DataUniqueTag(
            span_id="span",
            business_object_keys=["INV-1", "PO-2"],
            sequence=3,
            input_context="in",
            agent_output="out",
            occurred_at=AT,
            agent_cid=A,
            config_cid=B,
            artifact_cids=[C.hex()],
        ),
        {
            "v": 1,
            "type": "DUT",
            "span_id": "span",
            "business_object_keys": ["INV-1", "PO-2"],
            "sequence": 3,
            "input_context": "in",
            "agent_output": "out",
            "occurred_at": AT,
        },
        {"agent": A, "config": B, "artifacts": frozenset({C})},
        {
            "span_id": "other",
            "business_object_keys": ["INV-2"],
            "sequence": 4,
            "input_context": "changed",
            "agent_output": "changed",
            "occurred_at": AT + timedelta(seconds=1),
            "agent_cid": B,
            "config_cid": C,
            "artifact_cids": [A.hex()],
        },
    ),
    (
        LineageTag(
            actor_id="actor",
            step_id="step",
            transformation="work",
            occurred_at=AT,
            prev_cids=[A.hex()],
            derived_from_cids=[B.hex()],
            dut_cid=C,
        ),
        {
            "v": 1,
            "type": "LT",
            "actor_id": "actor",
            "step_id": "step",
            "transformation": "work",
            "occurred_at": AT,
        },
        {"prev": frozenset({A}), "derived_from": frozenset({B}), "dut": C},
        {
            "actor_id": "other",
            "step_id": "other",
            "transformation": "other",
            "occurred_at": AT + timedelta(seconds=1),
            "prev_cids": [B.hex()],
            "derived_from_cids": [C.hex()],
            "dut_cid": A,
        },
    ),
    (
        Artifact(sha256=A, locator="s3://bucket/object"),
        {"v": 1, "type": "Artifact", "sha256": A.hex()},
        {},
        {"sha256": B},
    ),
]


@pytest.mark.parametrize(
    "row,content,links,changes", CONTRACTS, ids=[type(c[0]).__name__ for c in CONTRACTS]
)
def test_node_content_and_links(row, content, links, changes):
    assert nodes.to_node(row) == (content, links)
    cid = dag.compute_cid(content, links)
    assert dag.compute_cid(*nodes.to_node(row)) == cid
    for field, value in changes.items():
        changed = row.model_copy(deep=True)
        setattr(changed, field, value)
        assert dag.compute_cid(*nodes.to_node(changed)) != cid, field


@pytest.mark.parametrize(
    "row,field,value",
    [(CONTRACTS[0][0], "name", "renamed"), (CONTRACTS[5][0], "locator", "https://other/object")],
)
def test_mutable_annotations_do_not_change_identity(row, field, value):
    changed = row.model_copy(deep=True)
    setattr(changed, field, value)
    assert nodes.to_node(changed) == nodes.to_node(row)


@pytest.mark.parametrize(
    "row,field",
    [
        (CONTRACTS[3][0], "artifact_cids"),
        (CONTRACTS[4][0], "prev_cids"),
        (CONTRACTS[4][0], "derived_from_cids"),
    ],
)
def test_link_lists_are_sets(row, field):
    left, right = row.model_copy(deep=True), row.model_copy(deep=True)
    setattr(left, field, [A.hex(), B.hex(), A.hex()])
    setattr(right, field, [B.hex(), A.hex()])
    assert dag.compute_cid(*nodes.to_node(left)) == dag.compute_cid(*nodes.to_node(right))


def test_hex_helpers_preserve_sorted_targets():
    assert nodes.hex_cid_list({B, A}) == [A.hex(), B.hex()]
    assert nodes.cids_from_hex([B.hex(), A.hex(), B.hex()]) == frozenset({A, B})
    with pytest.raises(TypeError, match="no assembler for object vNone"):
        nodes.to_node(object())


def test_resolve_absent_node(session):
    assert nodes.get_row(session, A) is None
    assert nodes.resolve(session, A) is None
