"""The fast path through the SDK: register agents with their configs, create
an explicit root, write inline-DUT nodes, and hand results between graphs via
exact ``derived_from`` LT CIDs -- then verify the whole story.
"""

import pytest

boc = pytest.importorskip(
    "aits_client",
    reason="aits-client SDK is not installed; run `make regen` first",
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


def _root(lineage: boc.LineageApi, actor: str) -> boc.LineageRead:
    return lineage.create_root(
        boc.LineageRootCreate(actor_id=actor, step_id="s", transformation="root")
    )


def _inline_node(
    lineage: boc.LineageApi,
    root_cid: str,
    prev: list[str],
    agent: boc.AgentRead,
    output: str,
    **overrides,
) -> boc.LineageNodeRead:
    payload = {
        "actor_id": agent.name,
        "step_id": "s",
        "transformation": "work",
        "prev": prev,
        "dut": boc.InlineDUTCreate(
            agent_uuid=agent.uuid,
            span_id=f"span-{output[:8]}",
            input_context=f"task for {agent.name}",
            agent_output=output,
        ),
        **overrides,
    }
    return lineage.create_lineage_node(root_cid, boc.LineageNodeCreate(**payload))


def test_explicit_root_is_the_entry_point(
    agents: boc.AgentsApi, lineage: boc.LineageApi, audit: boc.AuditApi
) -> None:
    """Keep root and parent selection with the caller across successive writes."""
    agent = _register(agents, "solo")
    root = _root(lineage, agent.name)
    first = _inline_node(lineage, root.cid, [root.cid], agent, "total = 4400.00 USD")
    assert first.root == root.cid
    assert first.node.root == root.cid
    assert first.dut is not None
    assert first.dut.config_cid == agent.active_config_cid

    second = _inline_node(lineage, root.cid, [first.node.cid], agent, "done")
    assert second.node.root == first.node.root
    assert second.node.prev == [first.node.cid]

    other = _root(lineage, agent.name)
    assert other.cid != root.cid
    assert audit.verify(second.node.cid).valid is True


def test_fast_path_end_to_end(
    agents: boc.AgentsApi, lineage: boc.LineageApi, audit: boc.AuditApi
) -> None:
    """Verify and trace a two-helper handoff using inline DUTs and exact source LT CIDs."""
    helper_a = _register(agents, "sub-A")
    helper_b = _register(agents, "sub-B")
    supervisor = _register(agents, "super")
    assert helper_a.active_config_cid is not None

    root_a = _root(lineage, helper_a.name)
    root_b = _root(lineage, helper_b.name)
    head_a = _inline_node(lineage, root_a.cid, [root_a.cid], helper_a, "PO-88")
    head_b = _inline_node(lineage, root_b.cid, [root_b.cid], helper_b, "ACME Corp")
    assert head_a.dut is not None
    assert head_a.dut.config_cid == helper_a.active_config_cid

    super_root = _root(lineage, supervisor.name)
    merge = _inline_node(
        lineage,
        super_root.cid,
        [super_root.cid],
        supervisor,
        "pay ACME Corp 4400 ref PO-88",
        transformation="Summarization",
        derived_from=[head_a.node.cid, head_b.node.cid],
    )
    assert sorted(merge.node.derived_from) == sorted([head_a.node.cid, head_b.node.cid])

    resolved = lineage.get_lineage_node(merge.node.cid)
    assert resolved.root == super_root.cid
    assert resolved.cid == merge.node.cid

    verdict = audit.verify(merge.node.cid)
    assert verdict.valid is True

    steps = lineage.trace_back(merge.node.cid)
    assert {"PO-88", "ACME Corp"} <= {s.agent_output for s in steps}

    report = audit.full_audit()
    assert report.valid is True
