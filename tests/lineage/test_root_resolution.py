"""Root derivation through ``prev``: membership, cycles, hashing."""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session

from app import dag, ledger
from app.models import LineageTag
from app.nodes import to_node


def _lt(
    session: Session,
    *,
    transformation: str,
    occurred_at: datetime,
    prev: list[bytes] | None = None,
    derived_from: list[bytes] | None = None,
) -> LineageTag:
    row = LineageTag(
        actor_id="A",
        step_id="s",
        transformation=transformation,
        occurred_at=occurred_at,
        prev_cids=[c.hex() for c in (prev or [])],
        derived_from_cids=[c.hex() for c in (derived_from or [])],
    )
    stored, _ = ledger.record(session, row)
    return stored


def test_root_resolves_to_itself(session: Session) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    root = _lt(session, transformation="root", occurred_at=when)
    assert ledger.resolve_root(session, root.cid) == root.cid


def test_chain_resolves_to_the_root(session: Session) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    root = _lt(session, transformation="root", occurred_at=when)
    child = _lt(
        session,
        transformation="child",
        occurred_at=when.replace(minute=1),
        prev=[root.cid],
    )
    assert ledger.resolve_root(session, child.cid) == root.cid


def test_same_root_join_resolves(session: Session) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    root = _lt(session, transformation="root", occurred_at=when)
    left = _lt(session, transformation="left", occurred_at=when.replace(minute=1), prev=[root.cid])
    right = _lt(
        session, transformation="right", occurred_at=when.replace(minute=2), prev=[root.cid]
    )
    join = _lt(
        session,
        transformation="join",
        occurred_at=when.replace(minute=3),
        prev=[left.cid, right.cid],
    )
    assert ledger.resolve_root(session, join.cid) == root.cid


def test_cross_root_join_is_rejected(session: Session) -> None:
    """Reject ambiguous graph membership when stored parents lead to different roots."""
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a = _lt(session, transformation="a", occurred_at=when)
    b = _lt(session, transformation="b", occurred_at=when)
    join = LineageTag(
        actor_id="A",
        step_id="s",
        transformation="join",
        occurred_at=when.replace(minute=1),
        prev_cids=[a.cid.hex(), b.cid.hex()],
    )
    stored, _ = ledger.record(session, join)
    with pytest.raises(ledger.ConflictingRoots) as exc:
        ledger.resolve_root(session, stored.cid)
    assert exc.value.roots == frozenset({a.cid, b.cid})


def test_derived_from_does_not_change_membership(session: Session) -> None:
    """Use only prev ancestry for membership, keeping consumed source graphs separate."""
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    source = _lt(session, transformation="source", occurred_at=when)
    root = _lt(session, transformation="root", occurred_at=when)
    child = _lt(
        session,
        transformation="child",
        occurred_at=when.replace(minute=1),
        prev=[root.cid],
        derived_from=[source.cid],
    )
    assert ledger.resolve_root(session, child.cid) == root.cid
    grouped = ledger.graphs_by_root(session)
    assert source.cid not in {row.cid for row in grouped[root.cid]}
    assert child.cid in {row.cid for row in grouped[root.cid]}
    assert source.cid in {row.cid for row in grouped[source.cid]}


def test_deep_chain_resolve(session: Session) -> None:
    """Resolve a 120-node ancestry chain and repeat the lookup with the populated cache."""
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    root = _lt(session, transformation="root", occurred_at=when)
    prev = root.cid
    tip = root
    for i in range(120):
        tip = _lt(
            session,
            transformation=f"n{i}",
            occurred_at=when.replace(microsecond=i + 1),
            prev=[prev],
        )
        prev = tip.cid
    memo: dict[bytes, bytes] = {}
    assert ledger.resolve_root(session, tip.cid, memo=memo) == root.cid
    assert ledger.resolve_root(session, tip.cid, memo=memo) == root.cid


def test_shared_join_ancestry_is_memoized(session: Session) -> None:
    """Retain root resolutions for branches that share an ancestor."""
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    root = _lt(session, transformation="root", occurred_at=when)
    mid = _lt(session, transformation="mid", occurred_at=when.replace(minute=1), prev=[root.cid])
    left = _lt(session, transformation="left", occurred_at=when.replace(minute=2), prev=[mid.cid])
    right = _lt(session, transformation="right", occurred_at=when.replace(minute=3), prev=[mid.cid])
    join = _lt(
        session,
        transformation="join",
        occurred_at=when.replace(minute=4),
        prev=[left.cid, right.cid],
    )
    memo: dict[bytes, bytes] = {}
    assert ledger.resolve_root(session, join.cid, memo=memo) == root.cid
    assert memo[mid.cid] == root.cid
    assert memo[left.cid] == root.cid


def test_cycle_is_detected(session: Session) -> None:
    """Detect a cycle introduced directly into storage instead of following it indefinitely."""
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a = _lt(session, transformation="a", occurred_at=when)
    b = _lt(session, transformation="b", occurred_at=when.replace(minute=1), prev=[a.cid])
    a.prev_cids = [b.cid.hex()]
    session.add(a)
    session.commit()
    with pytest.raises(ledger.CyclicLineage):
        ledger.resolve_root(session, b.cid)


def test_missing_parent_is_reported(session: Session) -> None:
    """Identify the missing ancestor when a stored lineage graph contains a dangling link."""
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    dangling = bytes.fromhex("ab" * 32)
    row = LineageTag(
        actor_id="A",
        step_id="s",
        transformation="broken",
        occurred_at=when,
        prev_cids=[dangling.hex()],
    )
    stored, _ = ledger.record(session, row)
    with pytest.raises(ledger.MissingLineageTag) as exc:
        ledger.resolve_root(session, stored.cid)
    assert exc.value.cid == dangling


def test_lt_hash_omits_session_identity() -> None:
    when = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    row = LineageTag(
        actor_id="A",
        step_id="s",
        transformation="root",
        occurred_at=when,
    )
    content, _links = to_node(row)
    assert "session_uuid" not in content
    assert content["type"] == "LT"


def test_lt_link_order_and_duplicates_are_set_like() -> None:
    when = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    a, b = dag.compute_cid({"x": 1}), dag.compute_cid({"x": 2})
    left = LineageTag(
        actor_id="A",
        step_id="s",
        transformation="join",
        occurred_at=when,
        prev_cids=[a.hex(), b.hex(), a.hex()],
    )
    right = LineageTag(
        actor_id="A",
        step_id="s",
        transformation="join",
        occurred_at=when,
        prev_cids=[b.hex(), a.hex()],
    )
    assert dag.compute_cid(*to_node(left)) == dag.compute_cid(*to_node(right))


def test_graph_frontier_ignores_derived_from(session: Session) -> None:
    """Keep a consumed source node on its own frontier until a same-graph child uses it."""
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    source = _lt(session, transformation="source", occurred_at=when)
    root = _lt(session, transformation="root", occurred_at=when)
    child = _lt(
        session,
        transformation="child",
        occurred_at=when.replace(minute=1),
        prev=[root.cid],
        derived_from=[source.cid],
    )
    grouped = ledger.graphs_by_root(session)
    assert ledger.graph_frontier(grouped[root.cid]) == {child.cid}
    assert ledger.graph_frontier(grouped[source.cid]) == {source.cid}
