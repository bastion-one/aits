"""The verify surface: generic per-node verify, the full audit, the commit log."""

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import dag
from app.models import AgentConfig, CommitLogEntry, DataUniqueTag, LineageTag
from app.nodes import to_node
from tests.helpers import create_node, create_root, make_config, make_dut_chain


def _populate(client: TestClient) -> dict:
    agent, config, dut = make_dut_chain(client)
    genesis = create_root(client)
    created = create_node(client, genesis["cid"], [genesis["cid"]], dut_cid=dut["cid"])
    return {
        "agent": agent,
        "config": config,
        "dut": dut,
        "genesis": genesis,
        "tip": created["node"],
    }


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
    assert audit["nodes_verified"] == 5
    assert audit["commit_entries"] == 5  # one observation per submission


def test_full_audit_over_an_empty_ledger(client: TestClient) -> None:
    assert client.get("/audit/").json() == {
        "valid": True,
        "nodes_checked": 0,
        "nodes_verified": 0,
        "commit_entries": 0,
        "failures": [],
    }


def test_full_audit_accepts_repeated_observations(client: TestClient) -> None:
    graph = _populate(client)
    assert make_config(client)["cid"] == graph["config"]["cid"]
    report = client.get("/audit/").json()
    assert report["valid"] is True
    assert report["failures"] == []
    assert report["nodes_checked"] == 5
    assert report["commit_entries"] == 6


def test_audit_reports_a_tampered_node(client: TestClient, session: Session) -> None:
    """Surface a content-hash mismatch through the ledger-wide audit."""
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
    """Detect observation-log corruption even when stored node content is untouched."""
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
    # Each entry links to its predecessor through the predecessor's entry hash.
    for prev, current in zip(entries, entries[1:]):
        assert current["prev_hash"] == prev["entry_hash"]
    only_dut = client.get("/commits/", params={"cid": graph["dut"]["cid"]}).json()
    assert {e["cid"] for e in only_dut} == {graph["dut"]["cid"]}


def test_verify_and_commit_filter_accept_case_and_reject_whitespace(client: TestClient) -> None:
    graph = _populate(client)
    cid = graph["dut"]["cid"]
    assert client.get(f"/verify/{cid.upper()}/").json()["valid"] is True
    entries = client.get("/commits/", params={"cid": cid.upper()}).json()
    assert {e["cid"] for e in entries} == {cid}

    spaced = " ".join(cid[i : i + 2] for i in range(0, 64, 2))
    assert client.get(f"/verify/{spaced}/").status_code == 400
    assert client.get("/commits/", params={"cid": spaced}).status_code == 400


def test_audit_reports_a_node_inserted_outside_the_write_path(
    client: TestClient, session: Session
) -> None:
    """A well-formed node with a correct CID but no commit-log entry fails audit."""
    _populate(client)
    row = AgentConfig(system_prompt="smuggled", llm_config={}, tools=[], config_metadata={})
    content, links = to_node(row)
    row.cid = dag.compute_cid(content, links)
    session.add(row)
    session.commit()

    audit = client.get("/audit/").json()
    assert audit["valid"] is False
    unlogged = [f for f in audit["failures"] if f["property_violated"] == "unlogged_node"]
    assert [f["record_key"] for f in unlogged] == [row.cid.hex()]


def test_audit_reports_a_logged_node_deleted_from_storage(
    client: TestClient, session: Session
) -> None:
    """A commit-log entry whose node is gone fails audit."""
    graph = _populate(client)
    session.delete(session.get(AgentConfig, bytes.fromhex(graph["config"]["cid"])))
    session.commit()

    audit = client.get("/audit/").json()
    assert audit["valid"] is False
    missing = [f for f in audit["failures"] if f["property_violated"] == "missing_node"]
    assert [f["record_key"] for f in missing] == [graph["config"]["cid"]]


def test_audit_reports_a_malformed_node_instead_of_failing(
    client: TestClient, session: Session
) -> None:
    """A corrupt structural column is an audit finding, not a 500."""
    graph = _populate(client)
    row = session.get(LineageTag, bytes.fromhex(graph["tip"]["cid"]))
    row.prev_cids = ["zz"]
    session.add(row)
    session.commit()

    response = client.get("/audit/")
    assert response.status_code == 200
    audit = response.json()
    assert audit["valid"] is False
    malformed = [f for f in audit["failures"] if f["property_violated"] == "malformed_node"]
    assert [f["record_key"] for f in malformed] == [graph["tip"]["cid"]]
    assert audit["nodes_verified"] < audit["nodes_checked"]

    verdict = client.get(f"/verify/{graph['tip']['cid']}/")
    assert verdict.status_code == 200
    assert verdict.json()["valid"] is False
    assert verdict.json()["property_violated"] == "malformed_node"


def test_malformed_linked_node_is_reported_against_itself(
    client: TestClient, session: Session
) -> None:
    """The finding names the malformed node, not the well-formed tip that links it."""
    graph = _populate(client)
    dut = session.get(DataUniqueTag, bytes.fromhex(graph["dut"]["cid"]))
    dut.artifact_cids = ["zz"]
    session.add(dut)
    session.commit()

    audit = client.get("/audit/").json()
    malformed = [f for f in audit["failures"] if f["property_violated"] == "malformed_node"]
    assert {f["record_key"] for f in malformed} == {graph["dut"]["cid"]}

    verdict = client.get(f"/verify/{graph['tip']['cid']}/").json()
    assert verdict["valid"] is False
    assert verdict["property_violated"] == "malformed_node"
    assert graph["dut"]["cid"] in verdict["message"]
    assert graph["tip"]["cid"] not in verdict["message"]


def _over_nest_config(session: Session, cid_hex: str) -> None:
    """Tamper a stored config in place: 70-level metadata, same CID and log."""
    deep: dict = {}
    for _ in range(69):
        deep = {"a": deep}
    row = session.get(AgentConfig, bytes.fromhex(cid_hex))
    row.config_metadata = deep
    session.add(row)
    session.commit()


def test_verify_reports_over_nested_stored_content(client: TestClient, session: Session) -> None:
    graph = _populate(client)
    config_cid = graph["config"]["cid"]
    _over_nest_config(session, config_cid)

    direct = client.get(f"/verify/{config_cid}/")
    assert direct.status_code == 200, direct.text
    assert direct.json()["valid"] is False
    assert direct.json()["property_violated"] == "canonical_encoding_error"

    through = client.get(f"/verify/{graph['dut']['cid']}/")  # the DUT links the config
    assert through.status_code == 200, through.text
    assert through.json()["valid"] is False
    assert through.json()["property_violated"] == "canonical_encoding_error"
    assert config_cid in through.json()["message"]


def test_audit_reports_over_nested_content_and_keeps_checking(
    client: TestClient, session: Session
) -> None:
    graph = _populate(client)
    _over_nest_config(session, graph["config"]["cid"])
    # Independent defects checked after the config: a tampered lineage node
    # and a broken commit chain.
    tip = session.get(LineageTag, bytes.fromhex(graph["tip"]["cid"]))
    tip.transformation = "rewritten"
    entry = session.exec(select(CommitLogEntry).order_by(CommitLogEntry.seq)).first()
    entry.entry_hash = b"\xff" * len(entry.entry_hash)
    session.add_all([tip, entry])
    session.commit()

    response = client.get("/audit/")
    assert response.status_code == 200, response.text
    audit = response.json()
    assert audit["valid"] is False
    found = {(f["property_violated"], f["record_key"]) for f in audit["failures"]}
    assert ("canonical_encoding_error", graph["config"]["cid"]) in found
    assert ("cid_mismatch", graph["tip"]["cid"]) in found
    assert {p for p, _ in found} & {"entry_hash_mismatch", "chain_broken"}
