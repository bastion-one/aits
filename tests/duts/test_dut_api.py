"""DUT span node HTTP roundtrips: identity, dedup-vs-occurrence, two clocks."""

from fastapi.testclient import TestClient

from tests.helpers import make_agent, make_artifact, make_config, make_dut


def test_create_then_get(client: TestClient) -> None:
    agent = make_agent(client)
    config = make_config(client)
    dut = make_dut(client, agent["uuid"], config["cid"])
    assert len(dut["cid"]) == 64
    assert dut["agent_cid"] == agent["cid"]
    assert dut["config_cid"] == config["cid"]

    got = client.get(f"/duts/{dut['cid']}/").json()
    assert got == dut


def test_unknown_references_404(client: TestClient) -> None:
    agent = make_agent(client)
    config = make_config(client)
    base = {
        "agent_uuid": agent["uuid"],
        "config_cid": config["cid"],
        "span_id": "s",
        "sequence": 0,
        "input_context": "p",
        "agent_output": "a",
    }
    assert (
        client.post(
            "/duts/", json={**base, "agent_uuid": "00000000-0000-0000-0000-000000000000"}
        ).status_code
        == 404
    )
    assert client.post("/duts/", json={**base, "config_cid": "0" * 64}).status_code == 404
    assert client.post("/duts/", json={**base, "artifact_cids": ["0" * 64]}).status_code == 404


def test_naive_occurred_at_rejected(client: TestClient) -> None:
    agent = make_agent(client)
    config = make_config(client)
    r = client.post(
        "/duts/",
        json={
            "agent_uuid": agent["uuid"],
            "config_cid": config["cid"],
            "span_id": "s",
            "sequence": 0,
            "input_context": "p",
            "agent_output": "a",
            "occurred_at": "2026-06-10T09:00:00",
        },
    )
    assert r.status_code == 400


def test_replay_dedupes_node_but_counts_every_observation(client: TestClient) -> None:
    agent = make_agent(client)
    config = make_config(client)
    first = make_dut(client, agent["uuid"], config["cid"])
    replay = make_dut(client, agent["uuid"], config["cid"])
    assert replay["cid"] == first["cid"]

    observations = client.get("/commits/", params={"cid": first["cid"]}).json()
    assert len(observations) == 2
    assert observations[0]["entry_hash"] != observations[1]["entry_hash"]


def test_span_id_and_occurred_at_are_inside_the_cid(client: TestClient) -> None:
    agent = make_agent(client)
    config = make_config(client)
    base = make_dut(client, agent["uuid"], config["cid"])
    twin = make_dut(client, agent["uuid"], config["cid"], span_id="span-2")
    later = make_dut(client, agent["uuid"], config["cid"], occurred_at="2026-06-10T09:05:00+00:00")
    assert len({base["cid"], twin["cid"], later["cid"]}) == 3


def test_artifact_set_folds_into_the_cid(client: TestClient) -> None:
    agent = make_agent(client)
    config = make_config(client)
    art_a = make_artifact(client, b"bytes-a")
    art_b = make_artifact(client, b"bytes-b")

    bare = make_dut(client, agent["uuid"], config["cid"])
    linked = make_dut(
        client, agent["uuid"], config["cid"], artifact_cids=[art_a["cid"], art_b["cid"]]
    )
    assert linked["cid"] != bare["cid"]
    assert sorted(linked["artifact_cids"]) == sorted([art_a["cid"], art_b["cid"]])

    # set semantics: order and duplicates do not move the CID
    reordered = make_dut(
        client,
        agent["uuid"],
        config["cid"],
        artifact_cids=[art_b["cid"], art_a["cid"], art_a["cid"]],
    )
    assert reordered["cid"] == linked["cid"]
