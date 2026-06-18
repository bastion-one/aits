"""The fast path through the SDK: register agents with their configs, record
each round of work in one call, and hand results between agents via
``used_sessions`` -- then verify the whole story.
"""

from uuid import uuid4

import pytest

boc = pytest.importorskip(
    "bastion_one_client",
    reason="bastion-one-client SDK is not installed; run `make regen` first",
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def agents(api_client: boc.ApiClient) -> boc.AgentsApi:
    return boc.AgentsApi(api_client)


@pytest.fixture(scope="module")
def lineage(api_client: boc.ApiClient) -> boc.LineageApi:
    return boc.LineageApi(api_client)


@pytest.fixture(scope="module")
def audit(api_client: boc.ApiClient) -> boc.AuditApi:
    return boc.AuditApi(api_client)


def _register(agents: boc.AgentsApi, name: str) -> boc.AgentRead:
    return agents.register(
        boc.AgentCreate(
            name=name,
            config=boc.ConfigCreate(
                system_prompt=f"You are {name}.",
                llm_config={"model": "gpt-4", "temperature": 0.0},
            ),
        )
    )


def _record(
    lineage: boc.LineageApi,
    session_uuid: str,
    agent: boc.AgentRead,
    output: str,
    **overrides,
) -> boc.RecordRead:
    payload = {
        "agent_uuid": agent.uuid,
        "span_id": f"span-{output[:8]}",
        "input_context": f"task for {agent.name}",
        "agent_output": output,
        "transformation": "work",
        "actor_id": agent.name,
        "step_id": "s",
        **overrides,
    }
    return lineage.record(session_uuid, boc.RecordCreate(**payload))


def test_agent_is_the_entry_point(
    agents: boc.AgentsApi, lineage: boc.LineageApi, audit: boc.AuditApi
) -> None:
    """register -> agent-scoped record: no session bookkeeping at all."""
    agent = _register(agents, "solo")
    first = agents.record(
        agent.uuid,
        boc.AgentRecordCreate(
            span_id="r1",
            input_context="extract",
            agent_output="total = 4400.00 USD",
            transformation="invoice-extraction",
        ),
    )
    assert first.node.actor_id == "solo"
    assert first.dut.config_cid == agent.active_config_cid

    second = agents.record(
        agent.uuid,
        boc.AgentRecordCreate(
            span_id="r2",
            input_context="follow-up",
            agent_output="done",
            transformation="emit",
        ),
    )
    assert second.node.session_uuid == first.node.session_uuid
    assert second.node.prev == [first.node.cid]

    genesis = agents.new_session(agent.uuid, boc.NewSessionCreate())
    assert genesis.session_uuid != first.node.session_uuid
    assert audit.verify(second.node.cid).valid is True


def test_fast_path_end_to_end(
    agents: boc.AgentsApi, lineage: boc.LineageApi, audit: boc.AuditApi
) -> None:
    helper_a = _register(agents, "sub-A")
    helper_b = _register(agents, "sub-B")
    supervisor = _register(agents, "super")
    assert helper_a.active_config_cid is not None

    sid_a, sid_b = str(uuid4()), str(uuid4())
    head_a = _record(lineage, sid_a, helper_a, "PO-88")
    head_b = _record(lineage, sid_b, helper_b, "ACME Corp")
    assert head_a.dut.config_cid == helper_a.active_config_cid

    merge = _record(
        lineage,
        str(uuid4()),
        supervisor,
        "pay ACME Corp 4400 ref PO-88",
        transformation="Summarization",
        used_sessions=[sid_a, sid_b],
    )
    assert sorted(merge.node.derived_from) == sorted([head_a.node.cid, head_b.node.cid])

    verdict = audit.verify(merge.node.cid)
    assert verdict.valid is True

    steps = lineage.trace_back(merge.node.cid)
    assert {"PO-88", "ACME Corp"} <= {s.agent_output for s in steps}

    report = audit.full_audit()
    assert report.valid is True
