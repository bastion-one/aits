"""Session-DAG HTTP roundtrips: genesis, append, fork/join, frontier."""

from fastapi.testclient import TestClient

from tests.helpers import append_node, make_dut_chain, start_session


def test_session_genesis_and_get(client: TestClient) -> None:
    genesis = start_session(client)
    assert genesis["prev"] == []
    assert genesis["derived_from"] == []
    assert genesis["dut"] is None

    got = client.get(f"/lineage/nodes/{genesis['cid']}/").json()
    assert got == genesis

    session_read = client.get(f"/lineage/{genesis['session_uuid']}/").json()
    assert [n["cid"] for n in session_read["nodes"]] == [genesis["cid"]]
    assert session_read["frontier"] == [genesis["cid"]]


def test_chain_append_moves_the_frontier(client: TestClient) -> None:
    _, _, dut = make_dut_chain(client)
    genesis = start_session(client)
    sid = genesis["session_uuid"]
    e1 = append_node(client, sid, [genesis["cid"]], "extract", dut=dut["cid"])
    assert e1["prev"] == [genesis["cid"]]
    assert e1["dut"] == dut["cid"]

    frontier = client.get(f"/lineage/{sid}/").json()["frontier"]
    assert frontier == [e1["cid"]]


def test_fork_and_join(client: TestClient) -> None:
    _, _, dut = make_dut_chain(client)
    genesis = start_session(client)
    sid = genesis["session_uuid"]
    e1 = append_node(client, sid, [genesis["cid"]], "extract", dut=dut["cid"])
    e2a = append_node(client, sid, [e1["cid"]], "tool:po-lookup")
    e2b = append_node(client, sid, [e1["cid"]], "tool:vendor-lookup")

    mid_fork = client.get(f"/lineage/{sid}/").json()["frontier"]
    assert sorted(mid_fork) == sorted([e2a["cid"], e2b["cid"]])

    e3 = append_node(client, sid, [e2a["cid"], e2b["cid"]], "synthesize")
    assert sorted(e3["prev"]) == sorted([e2a["cid"], e2b["cid"]])
    after_join = client.get(f"/lineage/{sid}/").json()["frontier"]
    assert after_join == [e3["cid"]]


def test_append_validations(client: TestClient) -> None:
    genesis = start_session(client)
    other = start_session(client, actor="B")
    sid = genesis["session_uuid"]

    base = {"actor_id": "A", "step_id": "s", "transformation": "t"}
    # prev must exist
    r = client.post(f"/lineage/{sid}/append/", json={**base, "prev": ["0" * 64]})
    assert r.status_code == 404
    # prev must belong to this session
    r = client.post(f"/lineage/{sid}/append/", json={**base, "prev": [other["cid"]]})
    assert r.status_code == 400
    # derived_from must not be intra-session (that is what prev is for)
    r = client.post(
        f"/lineage/{sid}/append/",
        json={**base, "prev": [genesis["cid"]], "derived_from": [genesis["cid"]]},
    )
    assert r.status_code == 400
    # derived_from targets must exist
    r = client.post(
        f"/lineage/{sid}/append/",
        json={**base, "prev": [genesis["cid"]], "derived_from": ["0" * 64]},
    )
    assert r.status_code == 404
    # the produced dut must exist
    r = client.post(
        f"/lineage/{sid}/append/", json={**base, "prev": [genesis["cid"]], "dut": "0" * 64}
    )
    assert r.status_code == 404
    # prev may not be empty (genesis is POST /lineage/)
    r = client.post(f"/lineage/{sid}/append/", json={**base, "prev": []})
    assert r.status_code == 422


def test_identical_append_dedupes(client: TestClient) -> None:
    genesis = start_session(client)
    sid = genesis["session_uuid"]
    body = {
        "actor_id": "A",
        "step_id": "s",
        "transformation": "t",
        "prev": [genesis["cid"]],
        "occurred_at": "2026-06-10T09:01:00+00:00",
    }
    first = client.post(f"/lineage/{sid}/append/", json=body).json()
    again = client.post(f"/lineage/{sid}/append/", json=body).json()
    assert again["cid"] == first["cid"]
    observations = client.get("/commits/", params={"cid": first["cid"]}).json()
    assert len(observations) == 2


def test_unknown_session_404s(client: TestClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/lineage/{missing}/").status_code == 404
    assert client.get(f"/lineage/nodes/{'0' * 64}/").status_code == 404


def test_list_sessions(client: TestClient) -> None:
    assert client.get("/lineage/").json() == []

    first = start_session(client, occurred_at="2026-06-10T09:00:00+00:00")
    second = start_session(client, actor="B", occurred_at="2026-06-10T10:00:00+00:00")
    tip = append_node(
        client,
        first["session_uuid"],
        [first["cid"]],
        occurred_at="2026-06-10T11:00:00+00:00",
    )

    sessions = client.get("/lineage/").json()
    # most recently active first: first session's append at 11:00 beats 10:00
    assert [s["session_uuid"] for s in sessions] == [
        first["session_uuid"],
        second["session_uuid"],
    ]
    assert sessions[0]["nodes"] == 2
    assert sessions[0]["started_at"] == "2026-06-10T09:00:00Z"
    assert sessions[0]["last_occurred_at"] == "2026-06-10T11:00:00Z"
    assert sessions[0]["frontier"] == [tip["cid"]]
    assert sessions[1]["frontier"] == [second["cid"]]

    limited = client.get("/lineage/", params={"limit": 1}).json()
    assert [s["session_uuid"] for s in limited] == [first["session_uuid"]]
