"""Lineage-graph HTTP roundtrips: root, nodes, fork/join, frontier, errors."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from tests.helpers import create_node, create_root, inline_dut, make_dut_chain


def test_root_genesis_and_get(client: TestClient) -> None:
    genesis = create_root(client)
    assert genesis["prev"] == []
    assert genesis["derived_from"] == []
    assert genesis["dut"] is None
    assert genesis["root"] == genesis["cid"]

    got = client.get(f"/lineage/nodes/{genesis['cid']}/").json()
    assert got == genesis

    graph = client.get(f"/lineage/{genesis['cid']}/").json()
    assert graph["root"] == genesis["cid"]
    assert [n["cid"] for n in graph["nodes"]] == [genesis["cid"]]
    assert graph["frontier"] == [genesis["cid"]]


def test_chain_append_moves_the_frontier(client: TestClient) -> None:
    _, _, dut = make_dut_chain(client)
    genesis = create_root(client)
    root = genesis["cid"]
    created = create_node(client, root, [genesis["cid"]], "extract", dut_cid=dut["cid"])
    e1 = created["node"]
    assert created["root"] == root
    assert created["dut"]["cid"] == dut["cid"]
    assert e1["prev"] == [genesis["cid"]]
    assert e1["dut"] == dut["cid"]
    assert e1["root"] == root

    frontier = client.get(f"/lineage/{root}/").json()["frontier"]
    assert frontier == [e1["cid"]]


def test_fork_and_join(client: TestClient) -> None:
    _, _, dut = make_dut_chain(client)
    genesis = create_root(client)
    root = genesis["cid"]
    e1 = create_node(client, root, [genesis["cid"]], "extract", dut_cid=dut["cid"])["node"]
    e2a = create_node(client, root, [e1["cid"]], "tool:po-lookup")["node"]
    e2b = create_node(client, root, [e1["cid"]], "tool:vendor-lookup")["node"]

    mid_fork = client.get(f"/lineage/{root}/").json()["frontier"]
    assert sorted(mid_fork) == sorted([e2a["cid"], e2b["cid"]])

    e3 = create_node(client, root, [e2a["cid"], e2b["cid"]], "synthesize")["node"]
    assert sorted(e3["prev"]) == sorted([e2a["cid"], e2b["cid"]])
    after_join = client.get(f"/lineage/{root}/").json()["frontier"]
    assert after_join == [e3["cid"]]


def test_explicit_sibling_parents_after_frontier_advances(client: TestClient) -> None:
    """Allow callers to branch from an earlier node after newer work has been recorded."""
    genesis = create_root(client)
    root = genesis["cid"]
    first = create_node(client, root, [genesis["cid"]], "extract")["node"]
    later = create_node(client, root, [first["cid"]], "later")["node"]
    assert client.get(f"/lineage/{root}/").json()["frontier"] == [later["cid"]]
    sibling = create_node(client, root, [first["cid"]], "sibling")["node"]
    frontier = client.get(f"/lineage/{root}/").json()["frontier"]
    assert sorted(frontier) == sorted([later["cid"], sibling["cid"]])
    assert sibling["prev"] == [first["cid"]]


def test_node_create_validations(client: TestClient) -> None:
    """Enforce existing references and root boundaries before accepting a lineage node."""
    genesis = create_root(client)
    other = create_root(client, actor="B")
    root = genesis["cid"]

    base = {"actor_id": "A", "step_id": "s", "transformation": "t"}
    # Every prev target must exist.
    missing_prev = client.post(f"/lineage/{root}/nodes/", json={**base, "prev": ["0" * 64]})
    assert missing_prev.status_code == 404
    assert "prev" in missing_prev.json()["detail"]

    # Every prev parent must belong to the path root.
    cross = client.post(f"/lineage/{root}/nodes/", json={**base, "prev": [other["cid"]]})
    assert cross.status_code == 400
    assert other["cid"] in cross.json()["detail"]

    # Same-root ancestry belongs in prev, not derived_from.
    intra = client.post(
        f"/lineage/{root}/nodes/",
        json={**base, "prev": [genesis["cid"]], "derived_from": [genesis["cid"]]},
    )
    assert intra.status_code == 400
    assert genesis["cid"] in intra.json()["detail"]

    # Cross-root source LTs must exist.
    missing_derived = client.post(
        f"/lineage/{root}/nodes/",
        json={**base, "prev": [genesis["cid"]], "derived_from": ["0" * 64]},
    )
    assert missing_derived.status_code == 404
    assert "derived_from" in missing_derived.json()["detail"]

    # An attached DUT must already exist when supplied by CID.
    missing_dut = client.post(
        f"/lineage/{root}/nodes/",
        json={**base, "prev": [genesis["cid"]], "dut_cid": "0" * 64},
    )
    assert missing_dut.status_code == 404
    assert "dut_cid" in missing_dut.json()["detail"]

    # Only POST /lineage/ creates a root; node creation requires parents.
    empty_prev = client.post(f"/lineage/{root}/nodes/", json={**base, "prev": []})
    assert empty_prev.status_code == 422

    missing_field = client.post(f"/lineage/{root}/nodes/", json=base)
    assert missing_field.status_code == 422

    malformed = client.post(f"/lineage/{root}/nodes/", json={**base, "prev": ["not-a-cid"]})
    assert malformed.status_code == 400
    assert "prev" in malformed.json()["detail"]

    non_root_path = client.post(
        f"/lineage/{other['cid']}/nodes/",
        json={**base, "prev": [genesis["cid"]]},
    )
    # other is a root, so this is a cross-root prev (400), not a non-root path
    assert non_root_path.status_code == 400

    child = create_node(client, root, [genesis["cid"]])["node"]
    via_child = client.post(
        f"/lineage/{child['cid']}/nodes/",
        json={**base, "prev": [child["cid"]]},
    )
    assert via_child.status_code == 400
    assert "not a root LT" in via_child.json()["detail"]


def test_derived_from_rejects_dut_cid(client: TestClient) -> None:
    """Require provenance links to identify lineage nodes, not their attached DUTs."""
    _, _, dut = make_dut_chain(client)
    genesis = create_root(client)
    r = client.post(
        f"/lineage/{genesis['cid']}/nodes/",
        json={
            "actor_id": "A",
            "step_id": "s",
            "transformation": "t",
            "prev": [genesis["cid"]],
            "derived_from": [dut["cid"]],
        },
    )
    assert r.status_code == 404
    assert "derived_from" in r.json()["detail"]


def test_both_dut_inputs_are_400(client: TestClient) -> None:
    """Reject ambiguous DUT attachment without exposing the submitted prompt or output."""
    agent, _, dut = make_dut_chain(client)
    genesis = create_root(client)
    r = client.post(
        f"/lineage/{genesis['cid']}/nodes/",
        json={
            "actor_id": "A",
            "step_id": "s",
            "transformation": "t",
            "prev": [genesis["cid"]],
            "dut_cid": dut["cid"],
            "dut": {
                "agent_uuid": agent["uuid"],
                "span_id": "s",
                "input_context": "secret prompt",
                "agent_output": "secret completion",
            },
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "dut_cid and dut are mutually exclusive"
    assert "secret" not in r.text


def test_legacy_request_fields_are_rejected(client: TestClient) -> None:
    genesis = create_root(client)
    root_legacy = client.post(
        "/lineage/",
        json={
            "actor_id": "A",
            "step_id": "s",
            "transformation": "root",
            "session_uuid": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert root_legacy.status_code == 422

    node_legacy = client.post(
        f"/lineage/{genesis['cid']}/nodes/",
        json={
            "actor_id": "A",
            "step_id": "s",
            "transformation": "t",
            "prev": [genesis["cid"]],
            "used_sessions": [],
        },
    )
    assert node_legacy.status_code == 422


def test_validation_errors_do_not_echo_inline_dut_text(client: TestClient) -> None:
    genesis = create_root(client)
    r = client.post(
        f"/lineage/{genesis['cid']}/nodes/",
        json={
            "actor_id": "A",
            "step_id": "s",
            "transformation": "t",
            "prev": [genesis["cid"]],
            "dut": {
                "agent_uuid": "not-a-uuid",
                "span_id": "s",
                "input_context": "INLINE-PROMPT-SHOULD-NOT-LEAK",
                "agent_output": "INLINE-COMPLETION-SHOULD-NOT-LEAK",
            },
        },
    )
    assert r.status_code == 422
    assert "INLINE-PROMPT-SHOULD-NOT-LEAK" not in r.text
    assert "INLINE-COMPLETION-SHOULD-NOT-LEAK" not in r.text


def test_identical_node_dedupes(client: TestClient) -> None:
    """Hold event time fixed so repeated node content resolves to the same CID."""
    genesis = create_root(client)
    root = genesis["cid"]
    body = {
        "actor_id": "A",
        "step_id": "s",
        "transformation": "t",
        "prev": [genesis["cid"]],
        "occurred_at": "2026-06-10T09:01:00+00:00",
    }
    first = client.post(f"/lineage/{root}/nodes/", json=body).json()
    again = client.post(f"/lineage/{root}/nodes/", json=body).json()
    assert again["node"]["cid"] == first["node"]["cid"]
    observations = client.get("/commits/", params={"cid": first["node"]["cid"]}).json()
    assert len(observations) == 2


def test_identical_roots_dedupe(client: TestClient) -> None:
    body = {
        "actor_id": "A",
        "step_id": "s",
        "transformation": "root",
        "occurred_at": "2026-06-10T09:00:00+00:00",
    }
    first = client.post("/lineage/", json=body).json()
    again = client.post("/lineage/", json=body).json()
    assert again["cid"] == first["cid"]


def test_cid_output_is_lowercase(client: TestClient) -> None:
    genesis = create_root(client)
    upper = genesis["cid"].upper()
    got = client.get(f"/lineage/nodes/{upper}/").json()
    assert got["cid"] == genesis["cid"]
    assert got["cid"] == got["cid"].lower()


def test_unknown_and_wrong_kind_404(client: TestClient) -> None:
    _, _, dut = make_dut_chain(client)
    assert client.get(f"/lineage/nodes/{'0' * 64}/").status_code == 404
    assert client.get(f"/lineage/{'0' * 64}/").status_code == 404
    assert client.get(f"/lineage/{dut['cid']}/").status_code == 404
    assert client.get(f"/lineage/nodes/{dut['cid']}/").status_code == 404


def test_removed_write_routes_are_gone(client: TestClient) -> None:
    genesis = create_root(client)
    assert client.post(f"/lineage/{genesis['cid']}/append/", json={}).status_code == 404
    assert client.post(f"/lineage/{genesis['cid']}/record/", json={}).status_code == 404
    agent = client.post("/agents/", json={"name": "x"}).json()
    assert client.post(f"/agents/{agent['uuid']}/record/", json={}).status_code == 404
    assert client.post(f"/agents/{agent['uuid']}/sessions/", json={}).status_code == 405


def test_node_create_sets_location(client: TestClient) -> None:
    genesis = create_root(client)
    r = client.post(
        f"/lineage/{genesis['cid']}/nodes/",
        json={
            "actor_id": "A",
            "step_id": "s",
            "transformation": "t",
            "prev": [genesis["cid"]],
        },
    )
    assert r.status_code == 201
    node = r.json()["node"]
    assert r.headers["location"] == f"/lineage/nodes/{node['cid']}/"


def test_list_roots(client: TestClient) -> None:
    """Summarize graph activity and frontier, ordering by latest activity before limiting."""
    assert client.get("/lineage/").json() == []

    first = create_root(client, occurred_at="2026-06-10T09:00:00+00:00")
    second = create_root(client, actor="B", occurred_at="2026-06-10T10:00:00+00:00")
    tip = create_node(
        client,
        first["cid"],
        [first["cid"]],
        occurred_at="2026-06-10T11:00:00+00:00",
    )["node"]

    graphs = client.get("/lineage/").json()
    # The first graph's activity at 11:00 puts it ahead of the root created at 10:00.
    assert [s["root"] for s in graphs] == [first["cid"], second["cid"]]
    assert graphs[0]["nodes"] == 2
    assert graphs[0]["started_at"] == "2026-06-10T09:00:00Z"
    assert graphs[0]["last_occurred_at"] == "2026-06-10T11:00:00Z"
    assert graphs[0]["frontier"] == [tip["cid"]]
    assert graphs[1]["frontier"] == [second["cid"]]

    limited = client.get("/lineage/", params={"limit": 1}).json()
    assert [s["root"] for s in limited] == [first["cid"]]


def test_summary_tie_breaks_on_root_cid(client: TestClient) -> None:
    when = "2026-06-10T09:00:00+00:00"
    first = create_root(client, actor="A", transformation="a", occurred_at=when)
    second = create_root(client, actor="B", transformation="b", occurred_at=when)
    graphs = client.get("/lineage/").json()
    expected = sorted([first["cid"], second["cid"]], reverse=True)
    assert [s["root"] for s in graphs] == expected


def test_graph_isolation(client: TestClient) -> None:
    a = create_root(client, actor="A")
    b = create_root(client, actor="B")
    create_node(client, a["cid"], [a["cid"]], actor="A")
    graph_a = client.get(f"/lineage/{a['cid']}/").json()
    graph_b = client.get(f"/lineage/{b['cid']}/").json()
    assert all(n["root"] == a["cid"] for n in graph_a["nodes"])
    assert [n["cid"] for n in graph_b["nodes"]] == [b["cid"]]
    assert a["cid"] not in {n["cid"] for n in graph_b["nodes"]}


def test_malformed_cid_matrix(client: TestClient) -> None:
    genesis = create_root(client)
    root = genesis["cid"]
    base = {"actor_id": "A", "step_id": "s", "transformation": "t", "prev": [root]}
    for value in ("a" * 63, "g" * 64):
        path_root = client.post(f"/lineage/{value}/nodes/", json=base)
        assert path_root.status_code == 400
        assert "root_cid" in path_root.json()["detail"]

        node = client.get(f"/lineage/nodes/{value}/")
        assert node.status_code == 400
        assert "lt_cid" in node.json()["detail"]

        derived = client.post(f"/lineage/{root}/nodes/", json={**base, "derived_from": [value]})
        assert derived.status_code == 400
        assert "derived_from" in derived.json()["detail"]

        dut = client.post(f"/lineage/{root}/nodes/", json={**base, "dut_cid": value})
        assert dut.status_code == 400
        assert "dut_cid" in dut.json()["detail"]


def test_prev_parents_with_different_roots_is_400(client: TestClient) -> None:
    a = create_root(client, actor="A")
    b = create_root(client, actor="B")
    child_a = create_node(client, a["cid"], [a["cid"]], actor="A")["node"]
    child_b = create_node(client, b["cid"], [b["cid"]], actor="B")["node"]
    r = client.post(
        f"/lineage/{a['cid']}/nodes/",
        json={
            "actor_id": "A",
            "step_id": "s",
            "transformation": "join",
            "prev": [child_a["cid"], child_b["cid"]],
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "prev" in detail
    assert child_b["cid"] in detail


def test_forward_dated_lineage_node_rejected(client: TestClient) -> None:
    genesis = create_root(client)
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    r = client.post(
        f"/lineage/{genesis['cid']}/nodes/",
        json={
            "actor_id": "A",
            "step_id": "s",
            "transformation": "t",
            "prev": [genesis["cid"]],
            "occurred_at": future,
        },
    )
    assert r.status_code == 400
    root = {"actor_id": "A", "step_id": "s", "transformation": "root", "occurred_at": future}
    assert client.post("/lineage/", json=root).status_code == 400


def test_forward_dated_inline_dut_rejected(client: TestClient) -> None:
    agent, config, _ = make_dut_chain(client)
    genesis = create_root(client)
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    r = client.post(
        f"/lineage/{genesis['cid']}/nodes/",
        json={
            "actor_id": "A",
            "step_id": "s",
            "transformation": "t",
            "prev": [genesis["cid"]],
            "dut": inline_dut(agent["uuid"], config_cid=config["cid"], occurred_at=future),
        },
    )
    assert r.status_code == 400
    assert "dut.occurred_at" in r.json()["detail"]
