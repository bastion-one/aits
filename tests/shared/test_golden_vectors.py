"""Golden vectors: the exact bytes of format 1.

Fixed inputs go through the public path (``nodes.to_node`` then
``dag.compute_cid``), so any change to an assembler, the encoder, the hash, or
the commit-log entry hash changes a constant below and fails this test. To
regenerate after a deliberate format change, run
``python -m tests.shared.test_golden_vectors`` and paste the output.
"""

from datetime import datetime, timezone
from uuid import UUID

import pytest

from app import dag, nodes
from app.ledger import ZERO_PREV
from app.models import (
    Agent,
    AgentConfig,
    Artifact,
    ConfigActivation,
    DataUniqueTag,
    LineageTag,
)
from tests.helpers import verify_reports_malformed_nodes

# Format 1 vectors. Regenerate only with a deliberate format change.
VECTORS = {
    "artifact": "af05f98a2e2caf0f32d9de2a70c700778c58ee830d4659eff72b447e5136ab94",
    "agent": "ed93eb855655a880708c78251a10d8c7ec39c5d16a6a5963421859472f74ad23",
    "config": "796f3911bb1a089afd0d707bba4f5ce7b14b5e553f4f07cac67fbbacd96d0c84",
    "config_numeric": "38a0f90ff53152152393b492932ff1cc6893728089aa3e58fb8f55f4901fd69d",
    "activation": "b16fb38fbc38cbfacbad3b1930a095c7a099a47bf2633c1a90eab2c83e004a48",
    "dut": "309b1c643ee72001d6cc554099839fae938ac3693edc2f76e4766404e084773d",
    "lineage": "3fb255e5a57079974037c9b8d1c095a7628856cd67bccfb9742d0318f9cefdf1",
    "entry_observe": "51c43facd9e3fbceeef439c47320817ad0813bde0207e853891266fbb5c55356",
}

AT = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
RECORDED_AT = datetime(2026, 1, 2, 3, 5, 0, tzinfo=timezone.utc)
AGENT_UUID = UUID("00000000-0000-4000-8000-000000000001")
LINK = dag.compute_cid({"link": "target"})  # any valid CID, whatever the CID format


def _cid(row) -> bytes:
    row.cid = dag.compute_cid(*nodes.to_node(row))
    return row.cid


def build() -> dict:
    """One node of each type, linked to each other, plus one entry hash."""
    artifact = Artifact(sha256=bytes(range(32)))
    agent = Agent(uuid=AGENT_UUID, created_at=AT, name="golden")
    config = AgentConfig(
        system_prompt="Extract the total.",
        llm_config={"model": "m", "temperature": 0.5},
        tools=["search"],
        config_metadata={"k": "v"},
    )
    cids = {"artifact": _cid(artifact), "agent": _cid(agent), "config": _cid(config)}
    # Integral float, exponent-form float, a key outside the BMP, negative zero:
    # cases where the pre-RFC 8785 encoder and RFC 8785 produce different bytes.
    numeric = AgentConfig(
        system_prompt="Extract the total.",
        llm_config={"temperature": 0.0, "top_p": 1e16, "\U0001f600": "non-bmp-key", "z": -0.0},
    )
    cids["config_numeric"] = _cid(numeric)
    activation = ConfigActivation(
        agent_uuid=AGENT_UUID, agent_cid=cids["agent"], config_cid=cids["config"]
    )
    dut = DataUniqueTag(
        span_id="span-1",
        business_object_keys=["INV-1"],
        sequence=3,
        input_context="in",
        agent_output="out",
        occurred_at=AT,
        agent_cid=cids["agent"],
        config_cid=cids["config"],
        artifact_cids=[cids["artifact"].hex()],
    )
    cids["activation"], cids["dut"] = _cid(activation), _cid(dut)
    lineage = LineageTag(
        actor_id="actor",
        step_id="step",
        transformation="root",
        occurred_at=AT,
        dut_cid=cids["dut"],
    )
    cids["lineage"] = _cid(lineage)
    out = {k: v.hex() for k, v in cids.items()}
    out["entry_observe"] = dag.commit_entry_hash(cids["agent"], RECORDED_AT, ZERO_PREV, "svc").hex()
    return out


def test_format_1_vectors():
    assert build() == VECTORS


@pytest.mark.parametrize(
    "row",
    [
        Agent(uuid=AGENT_UUID, created_at=AT, name="golden"),
        AgentConfig(system_prompt="p"),
        ConfigActivation(agent_uuid=AGENT_UUID, agent_cid=LINK, config_cid=LINK),
        DataUniqueTag(
            span_id="s",
            sequence=0,
            input_context="i",
            agent_output="o",
            occurred_at=AT,
            agent_cid=LINK,
            config_cid=LINK,
        ),
        LineageTag(actor_id="a", step_id="s", transformation="t", occurred_at=AT),
        Artifact(sha256=b"\x01" * 32),
    ],
    ids=lambda row: type(row).__name__,
)
def test_v_is_inside_the_hash(row):
    # pylint: disable=protected-access  # registers a temporary v2 assembler in _ASSEMBLERS
    row = row.model_copy(deep=True)
    v1 = dag.compute_cid(*nodes.to_node(row))
    assert nodes.to_node(row)[0]["v"] == 1
    row.v = 2
    with pytest.raises(TypeError, match=f"no assembler for {type(row).__name__} v2"):
        nodes.to_node(row)
    # Prove the version is hashed: give v2 an assembler that reuses v1's shape.
    nodes._ASSEMBLERS[(type(row), 2)] = nodes._ASSEMBLERS[(type(row), 1)]
    try:
        assert dag.compute_cid(*nodes.to_node(row)) != v1
    finally:
        del nodes._ASSEMBLERS[(type(row), 2)]


@pytest.mark.skipif(
    not verify_reports_malformed_nodes(),
    reason="dag.verify does not yet report malformed_node (audit/malformed-node-finding)",
)
def test_unknown_version_is_a_malformed_node(session):
    row = Agent(uuid=AGENT_UUID, created_at=AT, name="golden")
    row.cid = _cid(row)
    row.v = 2
    session.add(row)
    session.commit()

    result = nodes.verify_node(session, row.cid)
    assert result.property_violated == "malformed_node"
    assert result.record_key == row.cid.hex()


if __name__ == "__main__":
    for name, value in build().items():
        print(f'    "{name}": "{value}",')
