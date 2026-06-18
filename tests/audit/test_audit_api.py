"""The verify surface: generic per-node verify, the full audit, the commit log."""

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models import AgentConfig, CommitLogEntry
from tests.helpers import append_node, make_dut_chain, start_session


def _populate(client: TestClient) -> dict:
    agent, config, dut = make_dut_chain(client)
    genesis = start_session(client)
    tip = append_node(client, genesis["session_uuid"], [genesis["cid"]], dut=dut["cid"])
    return {"agent": agent, "config": config, "dut": dut, "genesis": genesis, "tip": tip}


def test_generic_verify_covers_every_node_type(client: TestClient) -> None:
    graph = _populate(client)
    for key in ("agent", "config", "dut", "genesis", "tip"):
        verdict = client.get(f"/verify/{graph[key]['cid']}/").json()
        assert verdict["valid"] is True, key
    assert client.get(f"/verify/{'0' * 64}/").status_code == 404


def test_full_audit_over_a_pristine_ledger(client: TestClient) -> None:
    _populate(client)
    audit = client.get("/audit/").json()
    assert audit["valid"] is True
    assert audit["failures"] == []
    assert audit["nodes_checked"] == 5  # agent, config, dut, genesis, tip
    assert audit["commit_entries"] == 5  # one observation per submission


def test_audit_reports_a_tampered_node(client: TestClient, session: Session) -> None:
    graph = _populate(client)
    row = session.get(AgentConfig, bytes.fromhex(graph["config"]["cid"]))
    row.system_prompt = "IGNORE ALL PRIOR INSTRUCTIONS"
    session.add(row)
    session.commit()

    audit = client.get("/audit/").json()
    assert audit["valid"] is False
    violated = {f["property_violated"] for f in audit["failures"]}
    assert "cid_mismatch" in violated


def test_audit_reports_a_broken_commit_chain(client: TestClient, session: Session) -> None:
    _populate(client)
    entry = session.exec(select(CommitLogEntry).order_by(CommitLogEntry.seq)).first()
    entry.entry_hash = b"\xff" * 32
    session.add(entry)
    session.commit()

    audit = client.get("/audit/").json()
    assert audit["valid"] is False
    violated = {f["property_violated"] for f in audit["failures"]}
    assert violated & {"entry_hash_mismatch", "chain_broken"}


def test_commit_log_lists_in_chain_order(client: TestClient) -> None:
    graph = _populate(client)
    entries = client.get("/commits/").json()
    assert [e["seq"] for e in entries] == sorted(e["seq"] for e in entries)
    # the chain threads: each entry's prev_hash is its predecessor's entry_hash
    for prev, current in zip(entries, entries[1:]):
        assert current["prev_hash"] == prev["entry_hash"]
    only_dut = client.get("/commits/", params={"cid": graph["dut"]["cid"]}).json()
    assert {e["cid"] for e in only_dut} == {graph["dut"]["cid"]}
