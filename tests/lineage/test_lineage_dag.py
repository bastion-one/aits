"""The merge workload: cross-session derived_from, watermark, traceback,
end-to-end tamper-evidence, and temporal hardening of merge edges."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import dag
from app.models import CommitLogEntry, DataUniqueTag
from tests.helpers import append_node, make_agent, make_config, make_dut, start_session


def _sub_agent_session(client: TestClient, actor: str, output: str) -> tuple[dict, dict]:
    """One sub-agent: genesis -> a round producing ``output``. Returns
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
    genesis = start_session(client, actor=actor)
    head = append_node(
        client, genesis["session_uuid"], [genesis["cid"]], "work", actor=actor, dut=dut["cid"]
    )
    return head, dut


def _supervisor_merge(client: TestClient, derived_from: list[str]) -> tuple[dict, dict]:
    """A supervisor session whose first round merges ``derived_from``."""
    agent = make_agent(client, name="super")
    config = make_config(client)
    dut = make_dut(
        client,
        agent["uuid"],
        config["cid"],
        span_id="span-super",
        agent_output="pay ACME Corp 4400.00 ref PO-88",
    )
    genesis = start_session(client, actor="super")
    merge = append_node(
        client,
        genesis["session_uuid"],
        [genesis["cid"]],
        "Summarization",
        actor="super",
        dut=dut["cid"],
        derived_from=derived_from,
    )
    return merge, dut


def test_merge_links_multiple_parents(client: TestClient) -> None:
    head_a, _ = _sub_agent_session(client, "sub-A", "PO-88")
    head_b, _ = _sub_agent_session(client, "sub-B", "ACME Corp")
    merge, _ = _supervisor_merge(client, [head_a["cid"], head_b["cid"]])
    assert sorted(merge["derived_from"]) == sorted([head_a["cid"], head_b["cid"]])
    assert client.get(f"/verify/{merge['cid']}/").json()["valid"] is True


def test_merge_pins_point_in_time_provenance(client: TestClient) -> None:
    head_a, dut_a = _sub_agent_session(client, "sub-A", "PO-88")
    merge, _ = _supervisor_merge(client, [head_a["cid"]])
    # A appends after the handoff; the merge still names the old head's CID
    late = append_node(
        client,
        head_a["session_uuid"],
        [head_a["cid"]],
        "late-append",
        actor="sub-A",
        dut=dut_a["cid"],
    )
    assert late["cid"] != head_a["cid"]
    assert client.get(f"/lineage/nodes/{merge['cid']}/").json()["derived_from"] == [head_a["cid"]]


def test_dut_granular_merge_is_allowed(client: TestClient) -> None:
    _, dut_a = _sub_agent_session(client, "sub-A", "PO-88")
    merge, _ = _supervisor_merge(client, [dut_a["cid"]])
    assert merge["derived_from"] == [dut_a["cid"]]
    assert client.get(f"/verify/{merge['cid']}/").json()["valid"] is True


def test_tampered_sub_agent_dut_breaks_the_supervisor_proof(
    client: TestClient, session: Session
) -> None:
    head_a, dut_a = _sub_agent_session(client, "sub-A", "PO-88")
    head_b, _ = _sub_agent_session(client, "sub-B", "ACME Corp")
    merge, _ = _supervisor_merge(client, [head_a["cid"], head_b["cid"]])

    row = session.get(DataUniqueTag, bytes.fromhex(dut_a["cid"]))
    row.agent_output = "POISONED"
    session.add(row)
    session.commit()

    verdict = client.get(f"/verify/{merge['cid']}/").json()
    assert verdict["valid"] is False
    assert verdict["property_violated"] == "cid_mismatch"


def test_watermark_renders_off_the_merge_node(client: TestClient) -> None:
    head_a, dut_a = _sub_agent_session(client, "sub-A", "PO-88")
    head_b, dut_b = _sub_agent_session(client, "sub-B", "ACME Corp")
    merge, _ = _supervisor_merge(client, [head_a["cid"], head_b["cid"]])

    wm = client.get(f"/lineage/nodes/{merge['cid']}/watermark/").json()["watermark"]
    assert "_Transformation:[Summarization]_" in wm
    # head-granular parents render as each head's produced DUT
    for dut in (dut_a, dut_b):
        assert dut["cid"][:8] in wm


def test_traceback_reaches_every_contributor_with_attribution(client: TestClient) -> None:
    head_a, _ = _sub_agent_session(client, "sub-A", "PO-88")
    head_b, _ = _sub_agent_session(client, "sub-B", "ACME Corp")
    merge, merge_dut = _supervisor_merge(client, [head_a["cid"], head_b["cid"]])

    steps = client.get(f"/lineage/nodes/{merge['cid']}/traceback/").json()
    outputs = {s["agent_output"] for s in steps}
    assert {"PO-88", "ACME Corp", merge_dut["agent_output"]} <= outputs
    assert all(s["agent_cid"] and s["config_cid"] for s in steps)
    occurred = [s["occurred_at"] for s in steps]
    assert occurred == sorted(occurred)


def test_backdated_merge_is_structurally_valid_but_temporally_flagged(
    client: TestClient, session: Session
) -> None:
    head_a, _ = _sub_agent_session(client, "sub-A", "PO-88")
    merge, _ = _supervisor_merge(client, [head_a["cid"]])

    # forge the ledger's observation of the merge to predate head_a's: rewrite
    # the merge's (final) commit entry, keeping the hash chain intact
    entries = session.exec(
        select(CommitLogEntry)
        .where(CommitLogEntry.cid == bytes.fromhex(merge["cid"]))
        .order_by(CommitLogEntry.seq)
    ).all()
    assert len(entries) == 1
    entry = entries[0]
    entry.recorded_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    entry.entry_hash = dag.commit_entry_hash(entry.cid, entry.recorded_at, entry.prev_hash)
    session.add(entry)
    session.commit()

    # the seal alone is still fine; the diary exposes the impossible timeline
    verdict = client.get(f"/verify/{merge['cid']}/").json()
    assert verdict["valid"] is False
    assert verdict["property_violated"] == "temporal_order"
    assert "first recorded" in verdict["message"]
