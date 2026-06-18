"""The agent as entry point: ``record`` against the current session, and
``new_session`` rotation of the pointer."""

from fastapi.testclient import TestClient

from tests.helpers import CONFIG_BODY, make_agent


def _register(client: TestClient, name: str = "worker") -> dict:
    r = client.post("/agents/", json={"name": name, "config": CONFIG_BODY})
    assert r.status_code == 201, r.text
    return r.json()


def _record(client: TestClient, agent: dict, **overrides) -> dict:
    payload = {
        "span_id": "span-1",
        "input_context": "find PO",
        "agent_output": "PO-88",
        "transformation": "find-po",
        **overrides,
    }
    r = client.post(f"/agents/{agent['uuid']}/record/", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_first_record_starts_the_current_session(client: TestClient) -> None:
    agent = _register(client)
    assert agent["current_session_uuid"] is None

    first = _record(client, agent)
    refreshed = client.get(f"/agents/{agent['uuid']}/").json()
    assert refreshed["current_session_uuid"] == first["node"]["session_uuid"]

    second = _record(client, agent, span_id="span-2")
    assert second["node"]["session_uuid"] == first["node"]["session_uuid"]
    assert second["node"]["prev"] == [first["node"]["cid"]]


def test_agent_defaults_for_actor_and_step(client: TestClient) -> None:
    agent = _register(client, name="invoice-extractor")
    round_ = _record(client, agent)
    assert round_["node"]["actor_id"] == "invoice-extractor"
    assert round_["node"]["step_id"] == "span-1"
    assert round_["dut"]["config_cid"] == agent["active_config_cid"]

    explicit = _record(client, agent, span_id="span-2", actor_id="me", step_id="extract")
    assert explicit["node"]["actor_id"] == "me"
    assert explicit["node"]["step_id"] == "extract"


def test_new_session_rotates_the_pointer(client: TestClient) -> None:
    agent = _register(client)
    first = _record(client, agent)
    old_session = first["node"]["session_uuid"]

    genesis = client.post(f"/agents/{agent['uuid']}/sessions/", json={})
    assert genesis.status_code == 201, genesis.text
    new_session = genesis.json()["session_uuid"]
    assert new_session != old_session
    assert genesis.json()["actor_id"] == agent["name"]

    rotated = _record(client, agent, span_id="span-2")
    assert rotated["node"]["session_uuid"] == new_session
    assert rotated["node"]["prev"] == [genesis.json()["cid"]]

    # the old session is untouched and still readable
    old = client.get(f"/lineage/{old_session}/").json()
    assert old["frontier"] == [first["node"]["cid"]]


def test_handoff_via_used_sessions(client: TestClient) -> None:
    helper = _register(client, name="sub-A")
    head = _record(client, helper, agent_output="PO-88")

    supervisor = _register(client, name="super")
    merge = _record(
        client,
        supervisor,
        span_id="s1",
        agent_output="pay ref PO-88",
        transformation="Summarization",
        used_sessions=[head["node"]["session_uuid"]],
    )
    assert merge["node"]["derived_from"] == [head["node"]["cid"]]
    assert client.get(f"/verify/{merge['node']['cid']}/").json()["valid"] is True


def test_record_without_active_config_leaves_no_pointer(client: TestClient) -> None:
    agent = make_agent(client)  # no config
    r = client.post(
        f"/agents/{agent['uuid']}/record/",
        json={
            "span_id": "x",
            "input_context": "x",
            "agent_output": "y",
            "transformation": "t",
        },
    )
    assert r.status_code == 409
    refreshed = client.get(f"/agents/{agent['uuid']}/").json()
    assert refreshed["current_session_uuid"] is None
