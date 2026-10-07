"""Agent genesis node HTTP roundtrips and the mutable name annotation."""

from fastapi.testclient import TestClient

from tests.helpers import CONFIG_BODY, make_agent, make_config


def test_register_returns_genesis_node(client: TestClient) -> None:
    agent = make_agent(client, name="InvoiceGen")
    assert len(agent["cid"]) == 64
    assert agent["uuid"]
    assert agent["created_at"]
    assert agent["name"] == "InvoiceGen"
    assert agent["active_config_cid"] is None

    got = client.get(f"/agents/{agent['uuid']}/").json()
    assert got == agent


def test_register_with_inline_config_activates_it(client: TestClient) -> None:
    r = client.post("/agents/", json={"name": "ready-to-run", "config": CONFIG_BODY})
    assert r.status_code == 201, r.text
    agent = r.json()
    assert agent["active_config_cid"] is not None

    active = client.get(f"/agents/{agent['uuid']}/active-config/").json()
    assert active["cid"] == agent["active_config_cid"]
    assert active["system_prompt"] == CONFIG_BODY["system_prompt"]
    history = client.get(f"/agents/{agent['uuid']}/activations/").json()
    assert len(history) == 1 and history[0]["prev_cid"] is None


def test_register_inline_config_dedupes_globally(client: TestClient) -> None:
    """Reuse the same configuration identity across standalone and inline creation."""
    standalone = make_config(client)
    r = client.post("/agents/", json={"name": "sharer", "config": CONFIG_BODY})
    assert r.json()["active_config_cid"] == standalone["cid"]


def test_two_agents_get_distinct_genesis_cids(client: TestClient) -> None:
    """Treat an agent name as a label, not a unique identity."""
    a = make_agent(client, name="same-name")
    b = make_agent(client, name="same-name")
    assert a["cid"] != b["cid"]
    assert a["uuid"] != b["uuid"]


def test_rename_overwrites_annotation_without_moving_the_cid(client: TestClient) -> None:
    """Allow a display-name change without invalidating the agent's stored proof."""
    agent = make_agent(client, name="before")
    renamed = client.patch(f"/agents/{agent['uuid']}/", json={"name": "after"}).json()
    assert renamed["name"] == "after"
    assert renamed["cid"] == agent["cid"]
    assert renamed["created_at"] == agent["created_at"]

    verdict = client.get(f"/verify/{agent['cid']}/").json()
    assert verdict["valid"] is True


def test_list_and_404(client: TestClient) -> None:
    make_agent(client, name="a")
    make_agent(client, name="b")
    assert {a["name"] for a in client.get("/agents/").json()} == {"a", "b"}
    missing = client.get("/agents/00000000-0000-0000-0000-000000000000/")
    assert missing.status_code == 404


def test_list_pages_in_creation_order(client: TestClient) -> None:
    names = [f"agent-{i}" for i in range(5)]
    for name in names:
        make_agent(client, name=name)
    first = client.get("/agents/", params={"limit": 2}).json()
    rest = client.get("/agents/", params={"limit": 10, "offset": 2}).json()
    assert [a["name"] for a in first + rest] == names


def test_list_rejects_out_of_range_paging(client: TestClient) -> None:
    for params in ({"limit": 0}, {"limit": 1001}, {"offset": -1}):
        assert client.get("/agents/", params=params).status_code == 422
