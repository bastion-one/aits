"""The pull reads for an external verifier: the ledger head and paging the commit log."""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from tests.helpers import make_agent, make_config


def _write(client: TestClient, count: int) -> None:
    for i in range(count):
        make_agent(client, name=f"agent-{i}")


def test_head_of_an_empty_log_is_all_null(client: TestClient) -> None:
    assert client.get("/ledger/head").json() == {
        "ledger_id": None,
        "seq": None,
        "entry_hash": None,
    }


def test_head_matches_the_first_and_last_commit_entries(client: TestClient) -> None:
    _write(client, 3)
    make_config(client)
    entries = client.get("/commits/").json()
    assert client.get("/ledger/head").json() == {
        "ledger_id": entries[0]["entry_hash"],
        "seq": entries[-1]["seq"],
        "entry_hash": entries[-1]["entry_hash"],
    }


def test_after_seq_pages_through_the_whole_log(client: TestClient) -> None:
    _write(client, 7)
    everything = client.get("/commits/").json()
    paged: list[dict] = []
    after_seq = None
    while True:
        params = {"limit": 3} if after_seq is None else {"limit": 3, "after_seq": after_seq}
        page = client.get("/commits/", params=params).json()
        if not page:
            break
        paged.extend(page)
        after_seq = page[-1]["seq"]
    assert paged == everything
    assert len({e["seq"] for e in paged}) == 7
    assert after_seq == client.get("/ledger/head").json()["seq"]


def test_after_seq_combines_with_the_cid_filter(client: TestClient) -> None:
    config = make_config(client)
    make_agent(client)
    make_config(client)  # a second observation of the same config
    observed = client.get("/commits/", params={"cid": config["cid"]}).json()
    assert len(observed) == 2
    later = client.get("/commits/", params={"cid": config["cid"], "after_seq": observed[0]["seq"]})
    assert later.json() == observed[1:]


def test_commit_limit_is_capped(client: TestClient) -> None:
    assert client.get("/commits/", params={"limit": 1000}).status_code == 200
    assert client.get("/commits/", params={"limit": 1001}).status_code == 422
    assert client.get("/commits/", params={"limit": 0}).status_code == 422


def test_head_requires_a_service_key(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "auth_disabled", False)
    monkeypatch.setattr(get_settings(), "auth_service_keys", "gateway:s3cret")
    assert client.get("/ledger/head").status_code == 401
    ok = client.get("/ledger/head", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
