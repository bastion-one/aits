"""Artifact node HTTP roundtrips: content addressing, locator, aliases."""

import io

from fastapi.testclient import TestClient

from app.hashing import sha256_hash
from tests.helpers import make_artifact

_OCTET = {"Content-Type": "application/octet-stream"}


def test_upload_hashes_the_bytes(client: TestClient) -> None:
    body = b"%PDF-1.7 ...invoice 4471..."
    artifact = make_artifact(client, body)
    assert artifact["sha256"] == sha256_hash(body).hex()
    assert len(artifact["cid"]) == 64
    assert artifact["locator"] is None
    assert artifact["aliases"] == []

    got = client.get(f"/artifacts/{artifact['cid']}/").json()
    assert got == artifact


def test_identical_bytes_dedupe_but_observations_count(client: TestClient) -> None:
    first = make_artifact(client, b"same-bytes")
    again = make_artifact(client, b"same-bytes")
    assert again["cid"] == first["cid"]
    observations = client.get("/commits/", params={"cid": first["cid"]}).json()
    assert len(observations) == 2


def test_lookup_by_byte_digest(client: TestClient) -> None:
    body = b"compare-these-bytes"
    artifact = make_artifact(client, body)
    found = client.get(f"/artifacts/by-digest/{sha256_hash(body).hex()}/").json()
    assert found == artifact

    unknown = sha256_hash(b"never-uploaded").hex()
    assert client.get(f"/artifacts/by-digest/{unknown}/").status_code == 404
    assert client.get("/artifacts/by-digest/not-hex/").status_code == 400


def test_locator_is_mutable_and_off_hash(client: TestClient) -> None:
    artifact = make_artifact(client, b"bytes")
    moved = client.patch(
        f"/artifacts/{artifact['cid']}/", json={"locator": "s3://bucket/key"}
    ).json()
    assert moved["locator"] == "s3://bucket/key"
    assert moved["cid"] == artifact["cid"]
    assert client.get(f"/verify/{artifact['cid']}/").json()["valid"] is True


def test_alias_roundtrip_and_conflict(client: TestClient) -> None:
    artifact = make_artifact(client, b"aliased-bytes")
    r = client.post(
        f"/artifacts/{artifact['cid']}/aliases/",
        json={"source": "anthropic", "alias": "file_abc123"},
    )
    assert r.status_code == 201, r.text

    resolved = client.get(
        "/artifacts/resolve/", params={"source": "anthropic", "alias": "file_abc123"}
    ).json()
    assert resolved["cid"] == artifact["cid"]

    # idempotent on the same pair
    again = client.post(
        f"/artifacts/{artifact['cid']}/aliases/",
        json={"source": "anthropic", "alias": "file_abc123"},
    )
    assert again.status_code == 201

    # the same pair on a different artifact conflicts
    other = make_artifact(client, b"other-bytes")
    clash = client.post(
        f"/artifacts/{other['cid']}/aliases/",
        json={"source": "anthropic", "alias": "file_abc123"},
    )
    assert clash.status_code == 409


def test_multipart_upload_registers_filename_alias(client: TestClient) -> None:
    body = b"%PDF-1.7 uploaded"
    r = client.post("/artifacts/upload/", files={"file": ("invoice_4471.pdf", io.BytesIO(body))})
    assert r.status_code == 201, r.text
    artifact = r.json()
    assert artifact["sha256"] == sha256_hash(body).hex()
    assert [(a["source"], a["alias"]) for a in artifact["aliases"]] == [
        ("upload", "invoice_4471.pdf")
    ]

    resolved = client.get(
        "/artifacts/resolve/", params={"source": "upload", "alias": "invoice_4471.pdf"}
    ).json()
    assert resolved["cid"] == artifact["cid"]


def test_unknown_artifact_404s(client: TestClient) -> None:
    assert client.get(f"/artifacts/{'0' * 64}/").status_code == 404
    assert (
        client.get("/artifacts/resolve/", params={"source": "x", "alias": "y"}).status_code == 404
    )
