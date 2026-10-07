"""Graph reads honor supplied snapshots and reuse ancestry caches."""

import pytest

from app import ledger, nodes
from app.models import DataUniqueTag, LineageTag
from tests.shared.test_golden_vectors import AT

A, B, C = (bytes([n]) * 32 for n in (1, 2, 3))


def _lt(cid, prev=(), dut=None):
    return LineageTag(
        cid=cid,
        actor_id="a",
        step_id="s",
        transformation="t",
        occurred_at=AT,
        prev_cids=[c.hex() for c in prev],
        dut_cid=dut,
    )


def test_grouping_uses_supplied_snapshot_without_database_reads(session, monkeypatch):
    root, child = _lt(A), _lt(B, [A])
    memo = {}

    def unexpected(*args):
        pytest.fail("a supplied graph snapshot must not query mutable database state")

    monkeypatch.setattr(session, "get", unexpected)
    grouped = ledger.graphs_by_root(session, rows=[child, root], memo=memo)
    assert grouped == {A: [child, root]}
    assert memo == {A: A, B: A}


def test_supplied_snapshot_cannot_resolve_a_parent_outside_it(session):
    root = _lt(A)
    session.add(root)
    session.commit()
    with pytest.raises(ledger.MissingLineageTag) as caught:
        ledger.graphs_by_root(session, rows=[_lt(B, [A])])
    assert caught.value.cid == A


@pytest.mark.parametrize("branch", ["missing", "cycle"])
def test_traceback_continues_after_missing_or_repeated_ancestry(session, monkeypatch, branch):
    # Whichever unordered target is popped first must not hide the other branch.
    last, first = list(nodes.cids_from_hex([A.hex(), B.hex()]))
    dut = DataUniqueTag(
        cid=C,
        span_id="s",
        sequence=0,
        input_context="i",
        agent_output="o",
        occurred_at=AT,
        agent_cid=A,
        config_cid=B,
    )
    rows = {C: _lt(C, [A, B]), last: _lt(last, dut=C)}
    if branch == "cycle":
        rows[first] = _lt(first, [C])
    calls = 0

    def get(table, cid):
        nonlocal calls
        calls += 1
        assert calls <= 10, "traceback must terminate on cyclic ancestry"
        return dut if table is DataUniqueTag else rows.get(cid)

    monkeypatch.setattr(session, "get", get)
    assert ledger.traceback(session, C) == [dut]


def test_router_root_resolution_preserves_supplied_cache(session):
    from app.routers.lineage import _resolve_root

    root, child = _lt(A), _lt(B, [A])
    session.add_all([root, child])
    session.commit()
    memo = {}
    assert _resolve_root(session, B, field="prev", memo=memo) == A
    assert memo == {A: A, B: A}
