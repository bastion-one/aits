"""The per-agent ConfigActivation chain: CAS appends, derived head, revert."""

from fastapi.testclient import TestClient

from tests.helpers import make_agent, make_config


def _activate(client: TestClient, agent_uuid: str, config_cid: str, expected_head: str | None):
    return client.post(
        f"/agents/{agent_uuid}/activations/",
        json={"config_cid": config_cid, "expected_head": expected_head},
    )


def test_first_activation_sets_the_active_config(client: TestClient) -> None:
    agent = make_agent(client)
    config = make_config(client)
    r = _activate(client, agent["uuid"], config["cid"], None)
    assert r.status_code == 201, r.text
    activation = r.json()
    assert activation["prev_cid"] is None
    assert activation["config_cid"] == config["cid"]

    active = client.get(f"/agents/{agent['uuid']}/active-config/").json()
    assert active["cid"] == config["cid"]


def test_stale_head_is_rejected(client: TestClient) -> None:
    agent = make_agent(client)
    config = make_config(client)
    first = _activate(client, agent["uuid"], config["cid"], None).json()

    # a concurrent writer that still believes there is no head
    assert _activate(client, agent["uuid"], config["cid"], None).status_code == 409
    # and one holding the real head succeeds
    other = make_config(client, system_prompt="other")
    assert _activate(client, agent["uuid"], other["cid"], first["cid"]).status_code == 201


def test_revert_reuses_the_config_cid_but_appends_a_new_activation(
    client: TestClient,
) -> None:
    agent = make_agent(client)
    y1 = make_config(client, system_prompt="v1")
    y2 = make_config(client, system_prompt="v2")

    a1 = _activate(client, agent["uuid"], y1["cid"], None).json()
    a2 = _activate(client, agent["uuid"], y2["cid"], a1["cid"]).json()
    # revert: same behavioral content dedupes to y1's CID...
    y1_again = make_config(client, system_prompt="v1")
    assert y1_again["cid"] == y1["cid"]
    # ...but the activation is a distinct chain link (identity from position)
    a3 = _activate(client, agent["uuid"], y1_again["cid"], a2["cid"]).json()
    assert a3["cid"] != a1["cid"]
    assert a3["config_cid"] == a1["config_cid"]

    history = client.get(f"/agents/{agent['uuid']}/activations/").json()
    assert [h["cid"] for h in history] == [a1["cid"], a2["cid"], a3["cid"]]
    assert [h["prev_cid"] for h in history] == [None, a1["cid"], a2["cid"]]

    active = client.get(f"/agents/{agent['uuid']}/active-config/").json()
    assert active["cid"] == y1["cid"]


def test_no_activation_means_no_active_config(client: TestClient) -> None:
    agent = make_agent(client)
    assert client.get(f"/agents/{agent['uuid']}/active-config/").status_code == 404
    assert client.get(f"/agents/{agent['uuid']}/activations/").json() == []


def test_set_config_dedupes_and_chains_without_cas(client: TestClient) -> None:
    agent = make_agent(client)
    first = client.put(f"/agents/{agent['uuid']}/config/", json={"system_prompt": "v1"})
    assert first.status_code == 200, first.text
    assert first.json()["prev_cid"] is None

    # setting the already-active config is a no-op: same activation back
    again = client.put(f"/agents/{agent['uuid']}/config/", json={"system_prompt": "v1"})
    assert again.json()["cid"] == first.json()["cid"]

    # a new config chains onto the head, no expected_head handshake needed
    second = client.put(f"/agents/{agent['uuid']}/config/", json={"system_prompt": "v2"})
    assert second.json()["prev_cid"] == first.json()["cid"]

    # revert: config CID reused, activation appended
    back = client.put(f"/agents/{agent['uuid']}/config/", json={"system_prompt": "v1"})
    assert back.json()["config_cid"] == first.json()["config_cid"]
    assert back.json()["prev_cid"] == second.json()["cid"]
    history = client.get(f"/agents/{agent['uuid']}/activations/").json()
    assert len(history) == 3


def test_activation_requires_known_agent_and_config(client: TestClient) -> None:
    agent = make_agent(client)
    config = make_config(client)
    missing_agent = _activate(client, "00000000-0000-0000-0000-000000000000", config["cid"], None)
    assert missing_agent.status_code == 404
    missing_config = _activate(client, agent["uuid"], "0" * 64, None)
    assert missing_config.status_code == 404
