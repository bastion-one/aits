"""A same-type CID collision fails closed: a submission whose CID is already
stored with different canonical bytes is rejected (409), never deduplicated.

SHA-256 cannot be collided on demand, so the hash is replaced with a stand-in
that keeps 32 bytes but only 16 bits of digest. The write path
(``ledger.sha256_hash``) and the verify path (``dag.compute_cid``) both use it.
"""

import hashlib
import logging

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import dag, ledger
from app.models import Artifact, CommitLogEntry

OCTETS = {"Content-Type": "application/octet-stream"}


def _weak_hash(data: bytes) -> bytes:
    return b"\x00" * 30 + hashlib.sha256(data).digest()[-2:]


def _weak_cid(content, links=None):
    return _weak_hash(dag.encode_node(content, links))


def _artifact_cid(body: bytes) -> bytes:
    return _weak_cid({"v": 1, "type": "Artifact", "sha256": hashlib.sha256(body).hexdigest()}, {})


def _twins() -> tuple[bytes, bytes]:
    benign = {_artifact_cid(b"approve $100 #%d" % i): i for i in range(2000)}
    for j in range(1_000_000):
        evil = b"approve $1,000,000 #%d" % j
        i = benign.get(_artifact_cid(evil))
        if i is not None:
            return b"approve $100 #%d" % i, evil
    raise RuntimeError("no collision found")


@pytest.fixture
def weak(monkeypatch):
    monkeypatch.setattr(ledger, "sha256_hash", _weak_hash)
    monkeypatch.setattr(dag, "compute_cid", _weak_cid)


def _collisions(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "aits.ledger" and "CID collision" in r.message]


def _entries(session: Session, cid: bytes) -> list[CommitLogEntry]:
    return session.exec(select(CommitLogEntry).where(CommitLogEntry.cid == cid)).all()


def test_same_type_collision_is_rejected(weak, client: TestClient, session: Session, caplog):
    benign, evil = _twins()
    assert _artifact_cid(benign) == _artifact_cid(evil)

    r1 = client.post("/artifacts/", content=benign, headers=OCTETS)
    with caplog.at_level(logging.WARNING, logger="aits.ledger"):
        r2 = client.post("/artifacts/", content=evil, headers=OCTETS)

    cid = r1.json()["cid"]
    assert (r1.status_code, r2.status_code) == (201, 409)
    assert r2.json()["detail"] == f"CID {cid} is already stored with different content"
    rows = session.exec(select(Artifact)).all()
    assert [r.sha256 for r in rows] == [hashlib.sha256(benign).digest()]
    assert len(_entries(session, bytes.fromhex(cid))) == 1
    assert len(_collisions(caplog)) == 1 and cid in _collisions(caplog)[0].message
    got = client.get(f"/artifacts/{cid}/")
    assert got.status_code == 200 and got.json() == r1.json()
    assert (
        client.get(f"/artifacts/by-digest/{hashlib.sha256(evil).hexdigest()}/").status_code == 404
    )


def test_identical_submissions_still_dedupe(weak, client: TestClient, session: Session, caplog):
    benign, evil = _twins()
    first = client.post("/artifacts/", content=benign, headers=OCTETS)
    client.post("/artifacts/", content=evil, headers=OCTETS)
    second = client.post("/artifacts/", content=benign, headers=OCTETS)

    cid = first.json()["cid"]
    assert (first.status_code, second.status_code) == (201, 201)
    assert second.json() == first.json()
    assert len(session.exec(select(Artifact)).all()) == 1
    assert len(_entries(session, bytes.fromhex(cid))) == 2  # benign twice; the collision none


def test_stage_raises_on_collision(weak, session: Session):
    benign, evil = _twins()
    stored, _ = ledger.stage(session, Artifact(sha256=hashlib.sha256(benign).digest()))

    with pytest.raises(ledger.CidCollision) as exc:
        ledger.stage(session, Artifact(sha256=hashlib.sha256(evil).digest()))

    assert exc.value.cid == stored.cid
    assert str(exc.value) == f"CID {stored.cid.hex()} is already stored with different content"
