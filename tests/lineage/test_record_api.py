"""The opinionated ``record`` entry point: one call per round of work."""

from uuid import uuid4

from fastapi.testclient import TestClient

from tests.helpers import CONFIG_BODY, make_agent, make_artifact, make_config


def _register(client: TestClient, name: str = "worker") -> dict:
    r = client.post("/agents/", json={"name": name, "config": CONFIG_BODY})
    assert r.status_code == 201, r.text
    return r.json()


def _record(client: TestClient, session_uuid: str, agent: dict, **overrides) -> dict:
    payload = {
        "agent_uuid": agent["uuid"],
        "span_id": "span-1",
        "input_context": "find PO",
        "agent_output": "PO-88",
        "transformation": "find-po",
        "actor_id": agent["name"],
        "step_id": "s",
        **overrides,
    }
    r = client.post(f"/lineage/{session_uuid}/record/", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_first_record_creates_the_session_genesis(client: TestClient) -> None:
    agent = _register(client)
    sid = str(uuid4())
    first = _record(client, sid, agent)

    session_read = client.get(f"/lineage/{sid}/").json()
    assert len(session_read["nodes"]) == 2  # genesis + the round
    genesis = next(n for n in session_read["nodes"] if not n["prev"])
    assert genesis["transformation"] == "session-start"
    assert first["node"]["prev"] == [genesis["cid"]]
    assert session_read["frontier"] == [first["node"]["cid"]]


def test_record_chains_onto_the_frontier(client: TestClient) -> None:
    agent = _register(client)
    sid = str(uuid4())
    first = _record(client, sid, agent)
    second = _record(client, sid, agent, span_id="span-2", agent_output="PO-88 final")
    assert second["node"]["prev"] == [first["node"]["cid"]]


def test_record_defaults_to_the_active_config(client: TestClient) -> None:
    agent = _register(client)
    round_ = _record(client, str(uuid4()), agent)
    assert round_["dut"]["config_cid"] == agent["active_config_cid"]

    # explicit config_cid overrides the default
    other = make_config(client, system_prompt="other recipe")
    explicit = _record(client, str(uuid4()), agent, span_id="span-x", config_cid=other["cid"])
    assert explicit["dut"]["config_cid"] == other["cid"]


def test_record_without_active_config_is_409(client: TestClient) -> None:
    agent = make_agent(client)  # no config
    r = client.post(
        f"/lineage/{uuid4()}/record/",
        json={
            "agent_uuid": agent["uuid"],
            "span_id": "s",
            "input_context": "x",
            "agent_output": "y",
            "transformation": "t",
            "actor_id": "a",
            "step_id": "s",
        },
    )
    assert r.status_code == 409
    assert "no active config" in r.json()["detail"]


def test_record_carries_artifacts_into_the_dut(client: TestClient) -> None:
    agent = _register(client)
    artifact = make_artifact(client, b"invoice bytes")
    round_ = _record(client, str(uuid4()), agent, artifact_cids=[artifact["cid"]])
    assert round_["dut"]["artifact_cids"] == [artifact["cid"]]
    assert client.get(f"/verify/{round_['node']['cid']}/").json()["valid"] is True


def test_explicit_prev_overrides_the_frontier(client: TestClient) -> None:
    agent = _register(client)
    sid = str(uuid4())
    first = _record(client, sid, agent)
    # fork: a second round explicitly chained onto the same node
    fork_a = _record(client, sid, agent, span_id="fork-a", prev=[first["node"]["cid"]])
    fork_b = _record(client, sid, agent, span_id="fork-b", prev=[first["node"]["cid"]])
    frontier = client.get(f"/lineage/{sid}/").json()["frontier"]
    assert sorted(frontier) == sorted([fork_a["node"]["cid"], fork_b["node"]["cid"]])
    # the next default-prev record joins the fork
    join = _record(client, sid, agent, span_id="join")
    assert sorted(join["node"]["prev"]) == sorted(frontier)


def test_used_sessions_link_the_source_frontiers(client: TestClient) -> None:
    helper_a = _register(client, "sub-A")
    helper_b = _register(client, "sub-B")
    sid_a, sid_b = str(uuid4()), str(uuid4())
    head_a = _record(client, sid_a, helper_a, agent_output="PO-88")
    head_b = _record(client, sid_b, helper_b, span_id="b1", agent_output="ACME Corp")

    supervisor = _register(client, "super")
    merge = _record(
        client,
        str(uuid4()),
        supervisor,
        span_id="s1",
        agent_output="pay ACME Corp 4400 ref PO-88",
        transformation="Summarization",
        used_sessions=[sid_a, sid_b],
    )
    assert sorted(merge["node"]["derived_from"]) == sorted(
        [head_a["node"]["cid"], head_b["node"]["cid"]]
    )

    steps = client.get(f"/lineage/nodes/{merge['node']['cid']}/traceback/").json()
    assert {"PO-88", "ACME Corp"} <= {s["agent_output"] for s in steps}
    assert client.get(f"/verify/{merge['node']['cid']}/").json()["valid"] is True


def test_used_sessions_validations(client: TestClient) -> None:
    agent = _register(client)
    sid = str(uuid4())
    _record(client, sid, agent)

    base = {
        "agent_uuid": agent["uuid"],
        "span_id": "s",
        "input_context": "x",
        "agent_output": "y",
        "transformation": "t",
        "actor_id": "a",
        "step_id": "s",
    }
    # a session cannot consume itself
    self_use = client.post(f"/lineage/{sid}/record/", json={**base, "used_sessions": [sid]})
    assert self_use.status_code == 400
    # unknown source session
    unknown = client.post(f"/lineage/{sid}/record/", json={**base, "used_sessions": [str(uuid4())]})
    assert unknown.status_code == 404
    # explicit empty prev is rejected
    empty_prev = client.post(f"/lineage/{sid}/record/", json={**base, "prev": []})
    assert empty_prev.status_code == 400
