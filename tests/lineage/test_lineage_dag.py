"""Cross-graph derived_from, watermark, traceback, tamper-evidence, and a
chain/fork/join graph with a later-advanced source that does not rewrite the
recorded exact LT reference.
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import dag
from app.models import CommitLogEntry, DataUniqueTag
from tests.helpers import create_node, create_root, make_agent, make_config, make_dut


def _sub_agent_graph(client: TestClient, actor: str, output: str) -> tuple[dict, dict]:
    """One sub-agent: root -> a round producing ``output``. Returns
    (head node, its DUT)."""
    agent = make_agent(client, name=actor)
    config = make_config(client)
    dut = make_dut(
        client,
        agent["uuid"],
        config["cid"],
        span_id=f"span-{actor}",
        agent_output=output,
    )
    genesis = create_root(client, actor=actor)
    created = create_node(
        client, genesis["cid"], [genesis["cid"]], "work", actor=actor, dut_cid=dut["cid"]
    )
    return created["node"], dut


def _supervisor_merge(client: TestClient, derived_from: list[str]) -> tuple[dict, dict]:
    """A supervisor graph whose first round merges ``derived_from``."""
    agent = make_agent(client, name="super")
    config = make_config(client)
    dut = make_dut(
        client,
        agent["uuid"],
        config["cid"],
        span_id="span-super",
        agent_output="pay ACME Corp 4400.00 ref PO-88",
    )
    genesis = create_root(client, actor="super")
    created = create_node(
        client,
        genesis["cid"],
        [genesis["cid"]],
        "Summarization",
        actor="super",
        dut_cid=dut["cid"],
        derived_from=derived_from,
    )
    return created["node"], dut


def test_merge_links_multiple_parents(client: TestClient) -> None:
    head_a, _ = _sub_agent_graph(client, "sub-A", "PO-88")
    head_b, _ = _sub_agent_graph(client, "sub-B", "ACME Corp")
    merge, _ = _supervisor_merge(client, [head_a["cid"], head_b["cid"]])
    assert sorted(merge["derived_from"]) == sorted([head_a["cid"], head_b["cid"]])
    assert client.get(f"/verify/{merge['cid']}/").json()["valid"] is True


def test_merge_pins_point_in_time_provenance(client: TestClient) -> None:
    head_a, dut_a = _sub_agent_graph(client, "sub-A", "PO-88")
    merge, _ = _supervisor_merge(client, [head_a["cid"]])
    # The source advances after handoff; the merge must retain the earlier LT CID.
    late = create_node(
        client,
        head_a["root"],
        [head_a["cid"]],
        "late-append",
        actor="sub-A",
        dut_cid=dut_a["cid"],
    )["node"]
    assert late["cid"] != head_a["cid"]
    assert client.get(f"/lineage/nodes/{merge['cid']}/").json()["derived_from"] == [head_a["cid"]]


def test_tampered_sub_agent_dut_breaks_the_supervisor_proof(
    client: TestClient, session: Session
) -> None:
    """Propagate evidence tampering through a cross-graph dependency to its consumer."""
    head_a, dut_a = _sub_agent_graph(client, "sub-A", "PO-88")
    head_b, _ = _sub_agent_graph(client, "sub-B", "ACME Corp")
    merge, _ = _supervisor_merge(client, [head_a["cid"], head_b["cid"]])

    row = session.get(DataUniqueTag, bytes.fromhex(dut_a["cid"]))
    row.agent_output = "POISONED"
    session.add(row)
    session.commit()

    verdict = client.get(f"/verify/{merge['cid']}/").json()
    assert verdict["valid"] is False
    assert verdict["property_violated"] == "cid_mismatch"


def test_watermark_renders_off_the_merge_node(client: TestClient) -> None:
    head_a, dut_a = _sub_agent_graph(client, "sub-A", "PO-88")
    head_b, dut_b = _sub_agent_graph(client, "sub-B", "ACME Corp")
    merge, _ = _supervisor_merge(client, [head_a["cid"], head_b["cid"]])

    wm = client.get(f"/lineage/nodes/{merge['cid']}/watermark/").json()["watermark"]
    assert "_Transformation:[Summarization]_" in wm
    # Each consumed LT is rendered using the CID of its attached DUT.
    for dut in (dut_a, dut_b):
        assert dut["cid"][:8] in wm


def test_traceback_reaches_every_contributor_with_attribution(client: TestClient) -> None:
    head_a, _ = _sub_agent_graph(client, "sub-A", "PO-88")
    head_b, _ = _sub_agent_graph(client, "sub-B", "ACME Corp")
    merge, merge_dut = _supervisor_merge(client, [head_a["cid"], head_b["cid"]])

    steps = client.get(f"/lineage/nodes/{merge['cid']}/traceback/").json()
    outputs = {s["agent_output"] for s in steps}
    assert {"PO-88", "ACME Corp", merge_dut["agent_output"]} <= outputs
    assert all(s["agent_cid"] and s["config_cid"] for s in steps)
    occurred = [s["occurred_at"] for s in steps]
    assert occurred == sorted(occurred)


def test_chain_fork_join_and_cross_root_derived_from(client: TestClient) -> None:
    """Follow source evidence in traceback without importing it into graph membership."""
    agent = make_agent(client, name="source")
    config = make_config(client)
    source_dut = make_dut(client, agent["uuid"], config["cid"], agent_output="upstream-fact")
    source_root = create_root(client, actor="source")
    source_head = create_node(
        client,
        source_root["cid"],
        [source_root["cid"]],
        "source-work",
        actor="source",
        dut_cid=source_dut["cid"],
    )["node"]

    root = create_root(client, actor="main")
    extract = create_node(client, root["cid"], [root["cid"]], "extract")["node"]
    fork_a = create_node(client, root["cid"], [extract["cid"]], "fork-a")["node"]
    fork_b = create_node(client, root["cid"], [extract["cid"]], "fork-b")["node"]
    join = create_node(
        client,
        root["cid"],
        [fork_a["cid"], fork_b["cid"]],
        "join",
        derived_from=[source_head["cid"]],
    )["node"]

    graph = client.get(f"/lineage/{root['cid']}/").json()
    graph_cids = {n["cid"] for n in graph["nodes"]}
    assert source_root["cid"] not in graph_cids
    assert source_head["cid"] not in graph_cids
    assert graph["frontier"] == [join["cid"]]
    assert join["derived_from"] == [source_head["cid"]]

    late_source = create_node(
        client, source_root["cid"], [source_head["cid"]], "source-later", actor="source"
    )["node"]
    assert client.get(f"/lineage/nodes/{join['cid']}/").json()["derived_from"] == [
        source_head["cid"]
    ]
    source_graph = client.get(f"/lineage/{source_root['cid']}/").json()
    assert late_source["cid"] in {n["cid"] for n in source_graph["nodes"]}
    assert late_source["cid"] not in {
        n["cid"] for n in client.get(f"/lineage/{root['cid']}/").json()["nodes"]
    }
    steps = client.get(f"/lineage/nodes/{join['cid']}/traceback/").json()
    assert "upstream-fact" in {s["agent_output"] for s in steps}


def test_backdated_merge_is_structurally_valid_but_temporally_flagged(
    client: TestClient, session: Session
) -> None:
    """Detect a merge observed before its source even when the commit hashes remain valid."""
    head_a, _ = _sub_agent_graph(client, "sub-A", "PO-88")
    merge, _ = _supervisor_merge(client, [head_a["cid"]])

    # Backdate the merge's final observation and recompute its entry hash so
    # the commit chain stays intact while the dependency timeline becomes invalid.
    entries = session.exec(
        select(CommitLogEntry)
        .where(CommitLogEntry.cid == bytes.fromhex(merge["cid"]))
        .order_by(CommitLogEntry.seq)
    ).all()
    assert len(entries) == 1
    entry = entries[0]
    entry.recorded_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    entry.entry_hash = dag.commit_entry_hash(
        entry.cid, entry.recorded_at, entry.prev_hash, entry.principal
    )
    session.add(entry)
    session.commit()

    verdict = client.get(f"/verify/{merge['cid']}/").json()
    assert verdict["valid"] is False
    # The hashes still match; observation order exposes the backdated merge.
    assert verdict["property_violated"] == "temporal_order"
    assert "first recorded" in verdict["message"]
