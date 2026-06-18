"""``GET /agents/{agent_uuid}/sessions/``: the sessions an agent produced work
in, resolved via the DUT ``agent_cid`` link, summarized like ``GET /lineage/``."""

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


def test_lists_the_agents_session_with_node_count(client: TestClient) -> None:
    agent = _register(client)
    first = _record(client, agent, occurred_at="2026-06-10T09:00:00+00:00")
    _record(client, agent, span_id="span-2", occurred_at="2026-06-10T09:05:00+00:00")

    sessions = client.get(f"/agents/{agent['uuid']}/sessions/").json()
    assert len(sessions) == 1
    assert sessions[0]["session_uuid"] == first["node"]["session_uuid"]
    assert sessions[0]["nodes"] == 3  # auto-started session-start node + 2 records
    assert sessions[0]["started_at"] == "2026-06-10T09:00:00Z"
    assert sessions[0]["last_occurred_at"] == "2026-06-10T09:05:00Z"


def test_no_records_returns_empty(client: TestClient) -> None:
    agent = _register(client, name="idle")
    assert client.get(f"/agents/{agent['uuid']}/sessions/").json() == []


def test_summary_covers_all_nodes_including_session_genesis(client: TestClient) -> None:
    agent = _register(client)
    _record(client, agent, occurred_at="2026-06-10T09:00:00+00:00")

    # Rotate to a fresh session (its genesis node carries no DUT), then record.
    genesis = client.post(f"/agents/{agent['uuid']}/sessions/", json={})
    assert genesis.status_code == 201, genesis.text
    new_uuid = genesis.json()["session_uuid"]
    _record(client, agent, span_id="span-2", occurred_at="2026-06-10T10:00:00+00:00")

    sessions = client.get(f"/agents/{agent['uuid']}/sessions/").json()
    by_uuid = {s["session_uuid"]: s for s in sessions}
    assert len(sessions) == 2
    # The rotated session is found via its record's DUT, but the summary counts
    # ALL its nodes: the dut-less genesis node + the record node.
    assert by_uuid[new_uuid]["nodes"] == 2
    # Most-recently-active first.
    assert sessions[0]["session_uuid"] == new_uuid


def test_one_agents_records_do_not_leak_into_another(client: TestClient) -> None:
    alpha = _register(client, name="alpha")
    beta = _register(client, name="beta")
    a_session = _record(client, alpha)["node"]["session_uuid"]
    b_session = _record(client, beta)["node"]["session_uuid"]

    alpha_sessions = [s["session_uuid"] for s in client.get(f"/agents/{alpha['uuid']}/sessions/").json()]
    beta_sessions = [s["session_uuid"] for s in client.get(f"/agents/{beta['uuid']}/sessions/").json()]
    assert alpha_sessions == [a_session]
    assert beta_sessions == [b_session]


def test_unknown_agent_is_404(client: TestClient) -> None:
    r = client.get("/agents/00000000-0000-0000-0000-000000000000/sessions/")
    assert r.status_code == 404
