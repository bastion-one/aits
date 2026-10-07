"""The temporal-order check fails closed: a node or link target with no
commit-log observation is a finding, on ``/verify/{cid}/`` and on ``/audit/``.

Each test tampers with the commit log and asserts the exact property reported.
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import dag
from app.ledger import check_temporal_order
from app.models import CommitLogEntry
from tests.lineage.test_lineage_dag import _sub_agent_graph, _supervisor_merge


def _entries(session: Session, cid_hex: str) -> list[CommitLogEntry]:
    return session.exec(
        select(CommitLogEntry).where(CommitLogEntry.cid == bytes.fromhex(cid_hex))
    ).all()


def _backdate(session: Session, cid_hex: str) -> None:
    """Record ``cid_hex`` as observed before anything it links, keeping its entry hash valid."""
    (entry,) = _entries(session, cid_hex)
    entry.recorded_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    entry.entry_hash = dag.commit_entry_hash(
        entry.cid, entry.recorded_at, entry.prev_hash, entry.principal
    )
    session.add(entry)


def _verify(client: TestClient, cid_hex: str) -> dict:
    return client.get(f"/verify/{cid_hex}/").json()


def _audit_findings(client: TestClient) -> list[tuple[str, str]]:
    audit = client.get("/audit/").json()
    assert audit["valid"] is False
    return sorted((f["property_violated"], f["record_key"]) for f in audit["failures"])


def test_backdated_merge_fails_temporal_order(client: TestClient, session: Session) -> None:
    """Control: a merge observed before its parent was already caught."""
    head_a, _ = _sub_agent_graph(client, "sub-A", "PO-88")
    merge, _ = _supervisor_merge(client, [head_a["cid"]])
    _backdate(session, merge["cid"])
    session.commit()

    verdict = _verify(client, merge["cid"])
    assert (verdict["valid"], verdict["property_violated"]) == (False, "temporal_order")
    assert _audit_findings(client) == [("temporal_order", merge["cid"])]


def test_deleting_the_parents_observation_does_not_hide_a_backdated_merge(
    client: TestClient, session: Session
) -> None:
    """The merge still fails temporal order against its other parents, the
    deleted parent observation is reported as unlogged, and the unlogged parent
    itself no longer verifies."""
    head_a, _ = _sub_agent_graph(client, "sub-A", "PO-88")
    merge, _ = _supervisor_merge(client, [head_a["cid"]])
    _backdate(session, merge["cid"])
    for entry in _entries(session, head_a["cid"]):
        session.delete(entry)
    session.commit()

    verdict = _verify(client, merge["cid"])
    assert (verdict["valid"], verdict["property_violated"]) == (False, "temporal_order")
    verdict = _verify(client, head_a["cid"])
    assert (verdict["valid"], verdict["property_violated"]) == (False, "unlogged_node")
    findings = _audit_findings(client)
    assert ("temporal_order", merge["cid"]) in findings
    assert findings.count(("unlogged_node", head_a["cid"])) == 1


def test_a_link_to_a_truncated_observation_is_unobserved(
    client: TestClient, session: Session
) -> None:
    """Truncating the log tail keeps the hash chain valid; a later merge that
    links the now-unobserved parent must not pass."""
    head_a, _ = _sub_agent_graph(client, "sub-A", "PO-88")
    (parent_entry,) = _entries(session, head_a["cid"])
    tail = session.exec(select(CommitLogEntry).order_by(CommitLogEntry.seq.desc())).first()
    assert tail.seq == parent_entry.seq
    session.delete(parent_entry)
    session.commit()
    merge, _ = _supervisor_merge(client, [head_a["cid"]])

    verdict = _verify(client, merge["cid"])
    assert (verdict["valid"], verdict["property_violated"]) == (False, "unobserved_link")
    assert head_a["cid"] in verdict["message"]
    # The API does not expose `actual`; the ledger result names child and parent.
    result = check_temporal_order(session, bytes.fromhex(merge["cid"]))
    assert (result.record_key, result.actual) == (merge["cid"], head_a["cid"])
    verdict = _verify(client, head_a["cid"])
    assert (verdict["valid"], verdict["property_violated"]) == (False, "unlogged_node")
    assert _audit_findings(client) == sorted(
        [("unlogged_node", head_a["cid"]), ("unobserved_link", merge["cid"])]
    )


def test_a_node_with_no_observation_is_unlogged(client: TestClient, session: Session) -> None:
    """The merge is the log tail, so deleting its entry keeps the chain valid."""
    head_a, _ = _sub_agent_graph(client, "sub-A", "PO-88")
    merge, _ = _supervisor_merge(client, [head_a["cid"]])
    for entry in _entries(session, merge["cid"]):
        session.delete(entry)
    session.commit()

    verdict = _verify(client, merge["cid"])
    assert (verdict["valid"], verdict["property_violated"]) == (False, "unlogged_node")
    assert _audit_findings(client) == [("unlogged_node", merge["cid"])]
