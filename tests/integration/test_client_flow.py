"""Step-wise integration test of the content-addressed flow: agent genesis ->
behavioral config -> activation -> artifact -> DUT span -> session DAG with a
two-sub-agent merge -> verify -> watermark -> traceback -> audit.

Hits the spawned ``app.main:app`` server (see this directory's ``conftest.py``)
through the generated Python SDK. Module-scoped resource fixtures chain prior
steps so the workflow runs once per module while each step's assertions stay
focused.
"""

import hashlib

import pytest

boc = pytest.importorskip(
    "bastion_one_client",
    reason="bastion-one-client SDK is not installed; run `make regen` first",
)

pytestmark = pytest.mark.integration

OCCURRED_AT = "2026-06-10T09:01:00+00:00"


# --- API handle fixtures -----------------------------------------------------


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


# --- workflow fixtures (each step's resource is the next step's input) -------


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


def _session_with_round(lineage: boc.LineageApi, actor: str, dut: boc.DUTRead) -> boc.LineageRead:
    genesis = lineage.start_session(boc.SessionCreate(actor_id=actor, step_id="s"))
    return lineage.append(
        genesis.session_uuid,
        boc.AppendCreate(
            actor_id=actor,
            step_id="s",
            transformation="work",
            prev=[genesis.cid],
            dut=dut.cid,
        ),
    )


@pytest.fixture(scope="module")
def head_a(
    duts: boc.DutsApi,
    lineage: boc.LineageApi,
    agent: boc.AgentRead,
    config: boc.ConfigRead,
) -> boc.LineageRead:
    return _session_with_round(
        lineage, "sub-A", _make_dut(duts, agent, config, span_id="a1", output="PO-88")
    )


@pytest.fixture(scope="module")
def head_b(
    duts: boc.DutsApi,
    lineage: boc.LineageApi,
    agent: boc.AgentRead,
    config: boc.ConfigRead,
) -> boc.LineageRead:
    return _session_with_round(
        lineage, "sub-B", _make_dut(duts, agent, config, span_id="b1", output="ACME Corp")
    )


@pytest.fixture(scope="module")
def merge(
    duts: boc.DutsApi,
    lineage: boc.LineageApi,
    agent: boc.AgentRead,
    config: boc.ConfigRead,
    artifact: boc.ArtifactRead,
    head_a: boc.LineageRead,
    head_b: boc.LineageRead,
) -> boc.LineageRead:
    merge_dut = _make_dut(
        duts,
        agent,
        config,
        span_id="s1",
        output="pay ACME Corp 4400.00 ref PO-88",
        artifact_cids=[artifact.cid],
    )
    genesis = lineage.start_session(boc.SessionCreate(actor_id="super", step_id="s"))
    return lineage.append(
        genesis.session_uuid,
        boc.AppendCreate(
            actor_id="super",
            step_id="s",
            transformation="Summarization",
            prev=[genesis.cid],
            derived_from=[head_a.cid, head_b.cid],
            dut=merge_dut.cid,
        ),
    )


# --- step-wise tests ---------------------------------------------------------


def test_create_agent(agent: boc.AgentRead, run_id: str) -> None:
    assert agent.cid
    assert agent.uuid
    assert agent.name == f"alpha-{run_id[:8]}"


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
    merge: boc.LineageRead, head_a: boc.LineageRead, head_b: boc.LineageRead
) -> None:
    assert sorted(merge.derived_from) == sorted([head_a.cid, head_b.cid])


def test_verify_recurses_the_whole_story(audit: boc.AuditApi, merge: boc.LineageRead) -> None:
    verdict = audit.verify(merge.cid)
    assert verdict.valid is True
    assert verdict.property_violated is None


def test_watermark_renders_off_the_merge(lineage: boc.LineageApi, merge: boc.LineageRead) -> None:
    wm = lineage.watermark(merge.cid).watermark
    assert "_Transformation:[Summarization]_" in wm


def test_traceback_reaches_both_sub_agents(
    lineage: boc.LineageApi, merge: boc.LineageRead, config: boc.ConfigRead
) -> None:
    steps = lineage.trace_back(merge.cid)
    outputs = {s.agent_output for s in steps}
    assert {"PO-88", "ACME Corp", "pay ACME Corp 4400.00 ref PO-88"} <= outputs
    assert all(s.config_cid == config.cid for s in steps)


def test_frontier_is_the_merge_node(lineage: boc.LineageApi, merge: boc.LineageRead) -> None:
    session_read = lineage.get_session_nodes(merge.session_uuid)
    assert session_read.frontier == [merge.cid]


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
