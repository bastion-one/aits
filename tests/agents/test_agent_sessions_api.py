"""``GET /agents/{agent_uuid}/sessions/``: graphs an agent produced work in,
resolved via DUT attachment, summarized like ``GET /lineage/``.
"""

from fastapi.testclient import TestClient

from tests.helpers import CONFIG_BODY, create_node, create_root, inline_dut


def _register(client: TestClient, name: str = "worker") -> dict:
    r = client.post("/agents/", json={"name": name, "config": CONFIG_BODY})
    assert r.status_code == 201, r.text
    return r.json()


def test_lists_the_agents_graph_with_node_count(client: TestClient) -> None:
    agent = _register(client)
    root = create_root(client, actor=agent["name"], occurred_at="2026-06-10T09:00:00+00:00")
    first = create_node(
        client,
        root["cid"],
        [root["cid"]],
        actor=agent["name"],
        occurred_at="2026-06-10T09:00:00+00:00",
        dut=inline_dut(agent["uuid"]),
    )["node"]
    create_node(
        client,
        root["cid"],
        [first["cid"]],
        actor=agent["name"],
        occurred_at="2026-06-10T09:05:00+00:00",
        dut=inline_dut(agent["uuid"], span_id="span-2"),
    )

    sessions = client.get(f"/agents/{agent['uuid']}/sessions/").json()
    assert len(sessions) == 1
    assert sessions[0]["root"] == root["cid"]
    assert sessions[0]["nodes"] == 3  # root + 2 DUT nodes
    assert sessions[0]["started_at"] == "2026-06-10T09:00:00Z"
    assert sessions[0]["last_occurred_at"] == "2026-06-10T09:05:00Z"


def test_no_records_returns_empty(client: TestClient) -> None:
    agent = _register(client, name="idle")
    assert client.get(f"/agents/{agent['uuid']}/sessions/").json() == []


def test_structural_actor_label_does_not_establish_participation(client: TestClient) -> None:
    """Require DUT attribution before listing a graph as work produced by an agent."""
    agent = _register(client)
    create_root(client, actor=agent["name"])
    assert client.get(f"/agents/{agent['uuid']}/sessions/").json() == []


def test_summary_covers_all_nodes_including_the_root(client: TestClient) -> None:
    agent = _register(client)
    first = create_root(client, actor=agent["name"], occurred_at="2026-06-10T09:00:00+00:00")
    create_node(
        client,
        first["cid"],
        [first["cid"]],
        actor=agent["name"],
        occurred_at="2026-06-10T09:00:00+00:00",
        dut=inline_dut(agent["uuid"]),
    )
    second = create_root(
        client, actor=agent["name"], transformation="job-2", occurred_at="2026-06-10T10:00:00+00:00"
    )
    create_node(
        client,
        second["cid"],
        [second["cid"]],
        actor=agent["name"],
        occurred_at="2026-06-10T10:00:00+00:00",
        dut=inline_dut(agent["uuid"], span_id="span-2"),
    )

    sessions = client.get(f"/agents/{agent['uuid']}/sessions/").json()
    by_root = {s["root"]: s for s in sessions}
    assert len(sessions) == 2
    # DUT attachment establishes participation, but the summary includes every
    # node in the graph: the root without a DUT and the node with a DUT.
    assert by_root[second["cid"]]["nodes"] == 2
    # List the most recently active graph first.
    assert sessions[0]["root"] == second["cid"]


def test_one_agents_records_do_not_leak_into_another(client: TestClient) -> None:
    alpha = _register(client, name="alpha")
    beta = _register(client, name="beta")
    a_root = create_root(client, actor="alpha")
    b_root = create_root(client, actor="beta")
    create_node(
        client, a_root["cid"], [a_root["cid"]], actor="alpha", dut=inline_dut(alpha["uuid"])
    )
    create_node(
        client,
        b_root["cid"],
        [b_root["cid"]],
        actor="beta",
        dut=inline_dut(beta["uuid"], span_id="b"),
    )

    alpha_roots = [s["root"] for s in client.get(f"/agents/{alpha['uuid']}/sessions/").json()]
    beta_roots = [s["root"] for s in client.get(f"/agents/{beta['uuid']}/sessions/").json()]
    assert alpha_roots == [a_root["cid"]]
    assert beta_roots == [b_root["cid"]]


def test_mixed_agent_participation_summarizes_the_whole_graph(client: TestClient) -> None:
    """Give each participating agent a summary that includes the other agent's work."""
    alpha = _register(client, name="alpha")
    beta = _register(client, name="beta")
    root = create_root(client, actor="alpha")
    first = create_node(
        client, root["cid"], [root["cid"]], actor="alpha", dut=inline_dut(alpha["uuid"])
    )["node"]
    create_node(
        client,
        root["cid"],
        [first["cid"]],
        actor="beta",
        dut=inline_dut(beta["uuid"], span_id="b"),
    )
    alpha_sessions = client.get(f"/agents/{alpha['uuid']}/sessions/").json()
    beta_sessions = client.get(f"/agents/{beta['uuid']}/sessions/").json()
    assert alpha_sessions[0]["root"] == root["cid"]
    assert beta_sessions[0]["root"] == root["cid"]
    assert alpha_sessions[0]["nodes"] == beta_sessions[0]["nodes"] == 3


def test_unknown_agent_is_404(client: TestClient) -> None:
    r = client.get("/agents/00000000-0000-0000-0000-000000000000/sessions/")
    assert r.status_code == 404
