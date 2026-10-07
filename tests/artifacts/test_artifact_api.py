"""Artifact node HTTP roundtrips: content addressing, locator, aliases."""

import io

from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

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
    """Allow storage locations to change without changing artifact identity or proof."""
    artifact = make_artifact(client, b"bytes")
    moved = client.patch(
        f"/artifacts/{artifact['cid']}/", json={"locator": "s3://bucket/key"}
    ).json()
    assert moved["locator"] == "s3://bucket/key"
    assert moved["cid"] == artifact["cid"]
    assert client.get(f"/verify/{artifact['cid']}/").json()["valid"] is True


def test_alias_roundtrip_and_conflict(client: TestClient) -> None:
    """Bind each source-and-alias pair to one artifact, with repeat registration allowed."""
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


def test_artifact_lookup_accepts_case_and_rejects_whitespace(client: TestClient) -> None:
    body = b"compare-case-bytes"
    artifact = make_artifact(client, body)
    digest = sha256_hash(body).hex()
    assert client.get(f"/artifacts/{artifact['cid'].upper()}/").json() == artifact
    assert client.get(f"/artifacts/by-digest/{digest.upper()}/").json() == artifact

    spaced_cid = " ".join(artifact["cid"][i : i + 2] for i in range(0, 64, 2))
    assert client.get(f"/artifacts/{spaced_cid}/").status_code == 400
    spaced_digest = " ".join(digest[i : i + 2] for i in range(0, 64, 2))
    assert client.get(f"/artifacts/by-digest/{spaced_digest}/").status_code == 400


def test_upload_hashes_a_multi_chunk_file(client: TestClient) -> None:
    body = bytes(range(256)) * (3 * 4096 + 7)  # a little over 3 MiB
    r = client.post("/artifacts/upload/", files={"file": ("big.bin", body)})
    assert r.status_code == 201, r.text
    assert r.json()["sha256"] == sha256_hash(body).hex()


def test_upload_reads_in_bounded_chunks(client: TestClient, monkeypatch) -> None:
    sizes: list[int] = []
    original = UploadFile.read

    async def recording_read(self, size: int = -1) -> bytes:
        sizes.append(size)
        return await original(self, size)

    monkeypatch.setattr(UploadFile, "read", recording_read)
    body = bytes(range(256)) * (3 * 4096 + 7)
    r = client.post("/artifacts/upload/", files={"file": ("big.bin", body)})
    assert r.status_code == 201, r.text
    assert r.json()["sha256"] == sha256_hash(body).hex()
    assert len(sizes) > 1
    assert all(0 < size <= 1024 * 1024 for size in sizes)
