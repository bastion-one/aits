"""Unit tests for the DAG primitive: CIDs, named edges, structural verify."""

from datetime import datetime, timezone

import pytest

from app import dag
from app.hashing import CHECKSUM_BYTES, ZERO_PREV, sha256_hash

CID_A = sha256_hash(b"a")
CID_B = sha256_hash(b"b")


def _store_resolver(store: dict):
    return store.get


def _put(store: dict, content, links=None) -> bytes:
    cid = dag.compute_cid(content, links)
    store[cid] = (content, dict(links or {}))
    return cid


def test_compute_cid_deterministic_and_content_sensitive() -> None:
    content = {"type": "X", "value": 1}
    cid = dag.compute_cid(content)
    assert len(cid) == CHECKSUM_BYTES
    assert dag.compute_cid({"value": 1, "type": "X"}) == cid
    assert dag.compute_cid({"type": "X", "value": 2}) != cid


def test_role_names_are_part_of_identity() -> None:
    content = {"type": "X"}
    assert dag.compute_cid(content, {"prev": CID_A}) != dag.compute_cid(
        content, {"derived_from": CID_A}
    )


def test_multi_valued_roles_fold_as_sets() -> None:
    content = {"type": "X"}
    assert dag.compute_cid(content, {"prev": {CID_A, CID_B}}) == dag.compute_cid(
        content, {"prev": [CID_B, CID_A, CID_A]}
    )


def test_empty_roles_hash_like_absent_roles() -> None:
    content = {"type": "X"}
    bare = dag.compute_cid(content)
    assert dag.compute_cid(content, {}) == bare
    assert dag.compute_cid(content, {"prev": set()}) == bare
    assert dag.compute_cid(content, {"prev": None}) == bare


def test_normalize_links_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        dag.normalize_links({"prev": b"short"})
    with pytest.raises(ValueError):
        dag.normalize_links({"prev": "not-bytes"})
    with pytest.raises(ValueError):
        dag.normalize_links({"": CID_A})


def test_iter_link_cids_flattens_roles() -> None:
    links = dag.normalize_links({"dut": CID_A, "prev": {CID_A, CID_B}})
    assert sorted(dag.iter_link_cids(links)) == sorted([CID_A, CID_A, CID_B])


def test_verify_ok_recurses_links() -> None:
    store: dict = {}
    leaf = _put(store, {"type": "leaf"})
    root = _put(store, {"type": "root"}, {"child": leaf})
    assert dag.verify(root, _store_resolver(store)).is_ok


def test_verify_reports_dangling_link() -> None:
    store: dict = {}
    leaf = _put(store, {"type": "leaf"})
    root = _put(store, {"type": "root"}, {"child": leaf})
    del store[leaf]
    result = dag.verify(root, _store_resolver(store))
    assert result.is_err
    assert result.property_violated == "dangling_link"
    assert result.record_key == leaf.hex()


def test_verify_reports_tampered_node() -> None:
    store: dict = {}
    leaf = _put(store, {"type": "leaf"})
    root = _put(store, {"type": "root"}, {"child": leaf})
    store[leaf] = ({"type": "leaf", "evil": True}, {})
    result = dag.verify(root, _store_resolver(store))
    assert result.is_err
    assert result.property_violated == "cid_mismatch"


def test_verify_memoizes_across_calls() -> None:
    store: dict = {}
    calls = 0

    def counting_resolver(cid):
        nonlocal calls
        calls += 1
        return store.get(cid)

    leaf = _put(store, {"type": "leaf"})
    left = _put(store, {"type": "L"}, {"next": leaf})
    right = _put(store, {"type": "R"}, {"next": leaf})
    root = _put(store, {"type": "join"}, {"prev": {left, right}})

    verified: set[bytes] = set()
    assert dag.verify(root, counting_resolver, verified=verified).is_ok
    assert calls == 4  # the diamond resolves each node once, not once per path
    assert verified == set(store)

    calls = 0
    assert dag.verify(root, counting_resolver, verified=verified).is_ok
    assert calls == 0  # warm cache: nothing re-resolved


def test_verify_failure_does_not_poison_the_cache() -> None:
    store: dict = {}
    leaf = _put(store, {"type": "leaf"})
    root = _put(store, {"type": "root"}, {"child": leaf})
    del store[leaf]
    verified: set[bytes] = set()
    assert dag.verify(root, _store_resolver(store), verified=verified).is_err
    assert not verified


def test_commit_entry_hash_covers_every_field() -> None:
    when = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    base = dag.commit_entry_hash(CID_A, when, ZERO_PREV)
    assert dag.commit_entry_hash(CID_A, when, ZERO_PREV) == base
    assert dag.commit_entry_hash(CID_B, when, ZERO_PREV) != base
    assert dag.commit_entry_hash(CID_A, when.replace(minute=1), ZERO_PREV) != base
    assert dag.commit_entry_hash(CID_A, when, sha256_hash(b"prev")) != base
