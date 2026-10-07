"""Callers select a root explicitly; agent identity has no current-graph pointer."""

from fastapi.testclient import TestClient

from tests.helpers import CONFIG_BODY, create_node, create_root, inline_dut, make_agent


def _register(client: TestClient, name: str = "worker") -> dict:
    r = client.post("/agents/", json={"name": name, "config": CONFIG_BODY})
    assert r.status_code == 201, r.text
    return r.json()


def test_agent_has_no_current_session_pointer(client: TestClient) -> None:
    agent = _register(client)
    assert "current_session_uuid" not in agent
    got = client.get(f"/agents/{agent['uuid']}/").json()
    assert "current_session_uuid" not in got


def test_one_agent_writes_two_explicit_roots(client: TestClient) -> None:
    """Attribute work in separate graphs to one unchanged agent identity."""
    agent = _register(client)
    first_root = create_root(client, actor=agent["name"], transformation="job-a")
    second_root = create_root(client, actor=agent["name"], transformation="job-b")
    a = create_node(
        client,
        first_root["cid"],
        [first_root["cid"]],
        actor=agent["name"],
        dut=inline_dut(agent["uuid"], span_id="a1"),
    )
    b = create_node(
        client,
        second_root["cid"],
        [second_root["cid"]],
        actor=agent["name"],
        dut=inline_dut(agent["uuid"], span_id="b1", agent_output="other"),
    )
    assert a["node"]["root"] == first_root["cid"]
    assert b["node"]["root"] == second_root["cid"]
    refreshed = client.get(f"/agents/{agent['uuid']}/").json()
    assert refreshed == agent or refreshed["cid"] == agent["cid"]
    assert refreshed["name"] == agent["name"]
    assert refreshed["active_config_cid"] == agent["active_config_cid"]


def test_handoff_via_exact_derived_from(client: TestClient) -> None:
    """Link a supervisor's work to the specific helper result it consumed."""
    helper = _register(client, name="sub-A")
    helper_root = create_root(client, actor="sub-A")
    head = create_node(
        client,
        helper_root["cid"],
        [helper_root["cid"]],
        actor="sub-A",
        dut=inline_dut(helper["uuid"], agent_output="PO-88"),
    )["node"]

    supervisor = _register(client, name="super")
    super_root = create_root(client, actor="super")
    merge = create_node(
        client,
        super_root["cid"],
        [super_root["cid"]],
        actor="super",
        transformation="Summarization",
        derived_from=[head["cid"]],
        dut=inline_dut(supervisor["uuid"], span_id="s1", agent_output="pay ref PO-88"),
    )
    assert merge["node"]["derived_from"] == [head["cid"]]
    assert client.get(f"/verify/{merge['node']['cid']}/").json()["valid"] is True


def test_removed_agent_write_routes(client: TestClient) -> None:
    """Prevent callers from using the former implicit session-writing API."""
    agent = make_agent(client)
    assert client.post(f"/agents/{agent['uuid']}/record/", json={}).status_code == 404
    assert client.post(f"/agents/{agent['uuid']}/sessions/", json={}).status_code == 405
