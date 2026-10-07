"""Step-wise integration test of the content-addressed flow: agent genesis ->
behavioral config -> activation -> artifact -> DUT span -> lineage DAG with a
two-sub-agent merge -> verify -> watermark -> traceback -> audit.

Hits the spawned ``app.main:app`` server (see this directory's ``conftest.py``)
through the generated Python SDK. Module-scoped resource fixtures chain prior
steps so the workflow runs once per module while each step's assertions stay
focused.
"""

import hashlib
from datetime import datetime

import pytest

boc = pytest.importorskip(
    "aits_client",
    reason="aits-client SDK is not installed; run `make regen` first",
)

pytestmark = pytest.mark.integration

OCCURRED_AT = "2026-06-10T09:01:00+00:00"


# API handle fixtures.


@pytest.fixture(scope="module")
def run_id() -> str:
    from uuid import uuid4

    return uuid4().hex


@pytest.fixture(scope="module")
def agents(api_client: boc.ApiClient) -> boc.AgentsApi:
    return boc.AgentsApi(api_client)


@pytest.fixture(scope="module")
def configs(api_client: boc.ApiClient) -> boc.ConfigsApi:
    return boc.ConfigsApi(api_client)


@pytest.fixture(scope="module")
def duts(api_client: boc.ApiClient) -> boc.DutsApi:
    return boc.DutsApi(api_client)


@pytest.fixture(scope="module")
def lineage(api_client: boc.ApiClient) -> boc.LineageApi:
    return boc.LineageApi(api_client)


@pytest.fixture(scope="module")
def artifacts(api_client: boc.ApiClient) -> boc.ArtifactsApi:
    return boc.ArtifactsApi(api_client)


@pytest.fixture(scope="module")
def audit(api_client: boc.ApiClient) -> boc.AuditApi:
    return boc.AuditApi(api_client)


# Workflow fixtures: each step supplies resources to the next step.


@pytest.fixture(scope="module")
def agent(agents: boc.AgentsApi, run_id: str) -> boc.AgentRead:
    return agents.register(boc.AgentCreate(name=f"alpha-{run_id[:8]}"))


@pytest.fixture(scope="module")
def config(configs: boc.ConfigsApi, run_id: str) -> boc.ConfigRead:
    return configs.create(
        boc.ConfigCreate(
            system_prompt="Extract the invoice total and pay the vendor.",
            llm_config={"model": "gpt-4", "temperature": 0.0},
            tools=[],
            metadata={"run_id": run_id},
        )
    )


@pytest.fixture(scope="module")
def activation(
    configs: boc.ConfigsApi, agent: boc.AgentRead, config: boc.ConfigRead
) -> boc.ActivationRead:
    return configs.activate(
        agent.uuid, boc.ActivationCreate(config_cid=config.cid, expected_head=None)
    )


@pytest.fixture(scope="module")
def artifact(artifacts: boc.ArtifactsApi, run_id: str) -> boc.ArtifactRead:
    return artifacts.upload(f"object-bytes-{run_id}".encode())


def _make_dut(
    duts: boc.DutsApi,
    agent: boc.AgentRead,
    config: boc.ConfigRead,
    *,
    span_id: str,
    output: str,
    artifact_cids: list[str] | None = None,
) -> boc.DUTRead:
    return duts.create(
        boc.DUTCreate(
            agent_uuid=agent.uuid,
            config_cid=config.cid,
            span_id=span_id,
            business_object_keys=["INV-4471"],
            sequence=0,
            input_context=f"task: {span_id}",
            agent_output=output,
            occurred_at=OCCURRED_AT,
            artifact_cids=artifact_cids or [],
        )
    )


def _graph_with_round(lineage: boc.LineageApi, actor: str, dut: boc.DUTRead) -> boc.LineageNodeRead:
    genesis = lineage.create_root(
        boc.LineageRootCreate(actor_id=actor, step_id="s", transformation="root")
    )
    return lineage.create_lineage_node(
        genesis.cid,
        boc.LineageNodeCreate(
            actor_id=actor,
            step_id="s",
            transformation="work",
            prev=[genesis.cid],
            dut_cid=dut.cid,
        ),
    )


@pytest.fixture(scope="module")
def head_a(
    duts: boc.DutsApi,
    lineage: boc.LineageApi,
    agent: boc.AgentRead,
    config: boc.ConfigRead,
) -> boc.LineageNodeRead:
    return _graph_with_round(
        lineage, "sub-A", _make_dut(duts, agent, config, span_id="a1", output="PO-88")
    )


@pytest.fixture(scope="module")
def head_b(
    duts: boc.DutsApi,
    lineage: boc.LineageApi,
    agent: boc.AgentRead,
    config: boc.ConfigRead,
) -> boc.LineageNodeRead:
    return _graph_with_round(
        lineage, "sub-B", _make_dut(duts, agent, config, span_id="b1", output="ACME Corp")
    )


@pytest.fixture(scope="module")
def merge(
    duts: boc.DutsApi,
    lineage: boc.LineageApi,
    agent: boc.AgentRead,
    config: boc.ConfigRead,
    artifact: boc.ArtifactRead,
    head_a: boc.LineageNodeRead,
    head_b: boc.LineageNodeRead,
) -> boc.LineageNodeRead:
    merge_dut = _make_dut(
        duts,
        agent,
        config,
        span_id="s1",
        output="pay ACME Corp 4400.00 ref PO-88",
        artifact_cids=[artifact.cid],
    )
    genesis = lineage.create_root(
        boc.LineageRootCreate(actor_id="super", step_id="s", transformation="root")
    )
    return lineage.create_lineage_node(
        genesis.cid,
        boc.LineageNodeCreate(
            actor_id="super",
            step_id="s",
            transformation="Summarization",
            prev=[genesis.cid],
            derived_from=[head_a.node.cid, head_b.node.cid],
            dut_cid=merge_dut.cid,
        ),
    )


@pytest.fixture(scope="module")
def structural(
    lineage: boc.LineageApi, merge: boc.LineageNodeRead
) -> tuple[boc.LineageRead, boc.LineageNodeRead]:
    """A structural node under a fresh root, plus HTTP-info for Location."""
    genesis = lineage.create_root(
        boc.LineageRootCreate(actor_id="struct", step_id="s", transformation="root")
    )
    created = lineage.create_lineage_node_with_http_info(
        genesis.cid,
        boc.LineageNodeCreate(
            actor_id="struct",
            step_id="s",
            transformation="note",
            prev=[genesis.cid],
        ),
    )
    return genesis, created


# Step-by-step workflow assertions.


def test_create_agent(agent: boc.AgentRead, run_id: str) -> None:
    assert agent.cid
    assert agent.uuid
    assert agent.name == f"alpha-{run_id[:8]}"
    assert (
        not hasattr(agent, "current_session_uuid")
        or getattr(agent, "current_session_uuid", None) is None
    )


def test_config_dedupes_on_content(
    configs: boc.ConfigsApi, config: boc.ConfigRead, run_id: str
) -> None:
    again = configs.create(
        boc.ConfigCreate(
            system_prompt="Extract the invoice total and pay the vendor.",
            llm_config={"model": "gpt-4", "temperature": 0.0},
            tools=[],
            metadata={"run_id": run_id},
        )
    )
    assert again.cid == config.cid


def test_activation_sets_the_active_config(
    configs: boc.ConfigsApi,
    agent: boc.AgentRead,
    config: boc.ConfigRead,
    activation: boc.ActivationRead,
) -> None:
    assert activation.prev_cid is None
    assert activation.config_cid == config.cid
    assert configs.active_config(agent.uuid).cid == config.cid
    history = configs.activation_history(agent.uuid)
    assert [a.cid for a in history] == [activation.cid]


def test_upload_artifact(artifact: boc.ArtifactRead, run_id: str) -> None:
    expected = hashlib.sha256(f"object-bytes-{run_id}".encode()).hexdigest()
    assert artifact.sha256 == expected
    assert artifact.cid
    assert artifact.aliases == []


def test_upload_file_captures_filename_alias(artifacts: boc.ArtifactsApi, run_id: str) -> None:
    """End-to-end proof the shimmed multipart client sends a real file part."""
    data = f"named-object-{run_id}".encode()
    filename = f"doc-{run_id[:6]}.pdf"
    art = artifacts.upload_file(file=(filename, data))
    assert art.sha256 == hashlib.sha256(data).hexdigest()
    assert ("upload", filename) in [(a.source, a.alias) for a in art.aliases]


def test_dut_replay_dedupes_but_observations_count(
    duts: boc.DutsApi,
    audit: boc.AuditApi,
    agent: boc.AgentRead,
    config: boc.ConfigRead,
) -> None:
    first = _make_dut(duts, agent, config, span_id="replayed", output="same")
    replay = _make_dut(duts, agent, config, span_id="replayed", output="same")
    assert replay.cid == first.cid
    observations = audit.commits(cid=first.cid)
    assert len(observations) == 2


def test_merge_links_both_sub_agents(
    merge: boc.LineageNodeRead, head_a: boc.LineageNodeRead, head_b: boc.LineageNodeRead
) -> None:
    assert sorted(merge.node.derived_from) == sorted([head_a.node.cid, head_b.node.cid])
    assert merge.dut is not None
    assert merge.node.dut == merge.dut.cid


def test_verify_recurses_the_whole_story(audit: boc.AuditApi, merge: boc.LineageNodeRead) -> None:
    """Verify the assembled workflow through the generated SDK and running HTTP service."""
    verdict = audit.verify(merge.node.cid)
    assert verdict.valid is True
    assert verdict.property_violated is None


def test_watermark_renders_off_the_merge(
    lineage: boc.LineageApi, merge: boc.LineageNodeRead
) -> None:
    wm = lineage.watermark(merge.node.cid).watermark
    assert "_Transformation:[Summarization]_" in wm


def test_traceback_reaches_both_sub_agents(
    lineage: boc.LineageApi, merge: boc.LineageNodeRead, config: boc.ConfigRead
) -> None:
    """Expose both consumed outputs and their configuration attribution through the SDK."""
    steps = lineage.trace_back(merge.node.cid)
    outputs = {s.agent_output for s in steps}
    assert {"PO-88", "ACME Corp", "pay ACME Corp 4400.00 ref PO-88"} <= outputs
    assert all(s.config_cid == config.cid for s in steps)


def test_frontier_is_the_merge_node(lineage: boc.LineageApi, merge: boc.LineageNodeRead) -> None:
    graph = lineage.get_lineage_graph(merge.root)
    assert graph.frontier == [merge.node.cid]
    assert graph.root == merge.root


def test_structural_node_has_null_dut_and_location(
    structural: tuple[boc.LineageRead, object],
) -> None:
    """Preserve an absent DUT and the created-node URL through SDK deserialization."""
    genesis, created = structural
    assert created.data.dut is None
    assert created.data.node.dut is None
    assert created.data.root == genesis.cid
    location = created.headers.get("Location") or created.headers.get("location")
    assert location == f"/lineage/nodes/{created.data.node.cid}/"


def test_inline_dut_mode(
    lineage: boc.LineageApi, duts: boc.DutsApi, agent: boc.AgentRead, config: boc.ConfigRead
) -> None:
    """Exercise SDK serialization of a nested DUT and read back its enclosing lineage node."""
    genesis = lineage.create_root(
        boc.LineageRootCreate(actor_id=agent.name, step_id="s", transformation="root")
    )
    created = lineage.create_lineage_node(
        genesis.cid,
        boc.LineageNodeCreate(
            actor_id=agent.name,
            step_id="s",
            transformation="inline",
            prev=[genesis.cid],
            occurred_at=OCCURRED_AT,
            dut=boc.InlineDUTCreate(
                agent_uuid=agent.uuid,
                span_id="inline-1",
                input_context="prompt",
                agent_output="completion",
                config_cid=config.cid,
                business_object_keys=["INV-4471", "PO-88"],
                occurred_at="2026-06-09T23:30:01.123456+00:00",
            ),
        ),
    )
    assert created.dut is not None
    assert created.dut.agent_output == "completion"
    assert created.dut.business_object_keys == ["INV-4471", "PO-88"]
    assert created.dut.occurred_at == datetime.fromisoformat("2026-06-09T23:30:01.123456+00:00")
    assert created.node.occurred_at == datetime.fromisoformat(OCCURRED_AT)
    assert duts.get(created.dut.cid) == created.dut
    resolved = lineage.get_lineage_node(created.node.cid)
    assert resolved.root == genesis.cid


def test_artifact_lookup_by_byte_digest(
    artifacts: boc.ArtifactsApi, artifact: boc.ArtifactRead
) -> None:
    found = artifacts.get_by_digest(artifact.sha256)
    assert found.cid == artifact.cid


@pytest.mark.usefixtures("merge")
def test_full_audit_is_clean(audit: boc.AuditApi) -> None:
    report = audit.full_audit()
    assert report.valid is True
    assert report.failures == []
    assert report.nodes_checked > 0
    assert report.commit_entries >= report.nodes_checked  # replays append extra entries
