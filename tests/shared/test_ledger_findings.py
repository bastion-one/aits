"""Forensic findings must preserve record identity, expected/actual values, and detail."""

from datetime import timedelta

import pytest
from sqlalchemy import text

from app import dag, ledger, nodes
from app.models import LineageTag
from app.result import Err
from tests.shared.test_golden_vectors import AT

A = b"\x01" * 32


def _root(session, step="root", prev=()):
    return ledger.record(
        session,
        LineageTag(
            actor_id="a",
            step_id=step,
            transformation="t",
            occurred_at=AT,
            prev_cids=[c.hex() for c in prev],
        ),
    )


def test_graph_error_keeps_the_cid_as_its_message():
    assert str(ledger.LineageGraphError(A)) == A.hex()


@pytest.mark.parametrize("tamper", ["prev", "hash"])
def test_commit_chain_findings_have_complete_detail(session, tamper):
    _, first = _root(session)
    _, second = _root(session, "second")
    if tamper == "prev":
        second.prev_hash = A
        expected = Err(
            "CommitLogEntry",
            second.seq,
            "chain_broken",
            first.entry_hash.hex(),
            A.hex(),
            f"commit-log entry {second.seq} does not chain to its predecessor",
        )
    else:
        second.entry_hash = A
        recomputed = dag.commit_entry_hash(
            second.cid, second.recorded_at, second.prev_hash, second.principal
        )
        expected = Err(
            "CommitLogEntry",
            second.seq,
            "entry_hash_mismatch",
            A.hex(),
            recomputed.hex(),
            f"commit-log entry {second.seq} no longer hashes to its stored entry_hash",
        )
    session.add(second)
    session.commit()
    assert ledger.verify_commit_log(session) == expected


def test_missing_observed_nodes_have_complete_sorted_findings(session):
    root, first = _root(session)
    child, second = _root(session, "child")
    assert ledger.check_coverage(session, {root.cid, child.cid}) == []
    # Deduplicated observations must produce one finding per missing node.
    _root(session)
    assert ledger.check_coverage(session, set()) == [
        Err(
            "CommitLogEntry",
            cid.hex(),
            "missing_node",
            cid.hex(),
            None,
            f"commit-log entry references {cid.hex()} but no node is stored",
        )
        for cid in sorted({first.cid, second.cid})
    ]


def test_missing_temporal_node_has_complete_detail(session):
    assert ledger.check_temporal_order(session, A) == Err(
        "node", A.hex(), "dangling_link", A.hex(), None, f"no node found at CID {A.hex()}"
    )


def test_unlogged_node_has_complete_detail(session):
    root, entry = _root(session)
    session.delete(entry)
    session.commit()
    assert ledger.check_temporal_order(session, root.cid) == Err(
        "node",
        root.cid.hex(),
        "unlogged_node",
        root.cid.hex(),
        None,
        f"node {root.cid.hex()} has no commit-log observation",
    )


def test_unobserved_link_has_complete_detail(session):
    child, _ = _root(session, "child", [A])
    assert ledger.check_temporal_order(session, child.cid) == Err(
        "node",
        child.cid.hex(),
        "unobserved_link",
        "a commit-log entry for the link target",
        A.hex(),
        f"link target {A.hex()} has no commit-log observation",
    )


def test_temporal_order_detail_and_equal_observation_times(session):
    root, parent_entry = _root(session)
    child, child_entry = _root(session, "child", [root.cid])
    parent_entry.recorded_at = child_entry.recorded_at
    session.add(parent_entry)
    session.commit()
    assert ledger.check_temporal_order(session, child.cid)
    parent_entry.recorded_at = child_entry.recorded_at + timedelta(seconds=1)
    session.add(parent_entry)
    session.commit()
    parent_time, child_time = (
        parent_entry.recorded_at.isoformat(),
        child_entry.recorded_at.isoformat(),
    )
    assert ledger.check_temporal_order(session, child.cid) == Err(
        "node",
        child.cid.hex(),
        "temporal_order",
        f"parent recorded <= {child_time}",
        parent_time,
        f"link target {root.cid.hex()} was first recorded at {parent_time}, "
        f"after this node's first observation at {child_time}",
    )


def test_first_observation_and_chain_are_explicitly_ordered(session):
    root, first = _root(session)
    _, replay = _root(session)
    replay.recorded_at = first.recorded_at + timedelta(seconds=1)
    replay.entry_hash = dag.commit_entry_hash(
        replay.cid, replay.recorded_at, replay.prev_hash, replay.principal
    )
    session.add(replay)
    session.commit()
    session.execute(text("PRAGMA reverse_unordered_selects = ON"))
    assert ledger.first_recorded_at(session, root.cid) == first.recorded_at
    assert ledger.first_recorded_at(session, A) is None
    assert ledger.verify_commit_log(session)


def test_node_verification_populates_and_reuses_the_supplied_cache(session, monkeypatch):
    root, _ = _root(session)
    verified = set()
    assert nodes.verify_node(session, root.cid, verified=verified)
    assert verified == {root.cid}

    def unexpected(*args):
        pytest.fail("a verified node must not be read again")

    monkeypatch.setattr(nodes, "resolve", unexpected)
    assert nodes.verify_node(session, root.cid, verified=verified)
