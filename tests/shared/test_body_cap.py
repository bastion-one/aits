"""Request bodies over MAX_REQUEST_BYTES are rejected with 413."""

import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.main import app

OCTETS = {"Content-Type": "application/octet-stream"}


@pytest.fixture
def small_cap(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(get_settings(), "max_request_bytes", 1024)
    return 1024


def test_body_under_the_cap_is_accepted(client: TestClient, small_cap: int) -> None:
    assert client.post("/artifacts/", content=b"x" * small_cap, headers=OCTETS).status_code == 201


def test_declared_oversize_body_is_a_413(client: TestClient, small_cap: int) -> None:
    r = client.post("/artifacts/", content=b"x" * (small_cap + 1), headers=OCTETS)
    assert r.status_code == 413


def test_streamed_oversize_body_without_a_length_is_a_413(
    client: TestClient, small_cap: int
) -> None:
    """A chunked body has no Content-Length; the cap is enforced while reading."""

    def chunks():
        for _ in range(4):
            yield b"x" * small_cap

    r = client.post("/artifacts/", content=chunks(), headers=OCTETS)
    assert r.status_code == 413


def test_oversize_multipart_upload_is_a_413(client: TestClient, small_cap: int) -> None:
    r = client.post("/artifacts/upload/", files={"file": ("big.bin", b"x" * (small_cap * 2))})
    assert r.status_code == 413


def test_declared_oversize_is_rejected_before_the_body_is_sent(small_cap: int) -> None:
    """TestClient always sends the whole body, so use a real socket: send the
    headers of an oversize request and no body; the 413 must still arrive."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        while not server.started:
            time.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
            conn.sendall(
                b"POST /artifacts/ HTTP/1.1\r\nHost: x\r\n"
                b"Content-Type: application/octet-stream\r\n"
                b"Content-Length: " + str(small_cap + 1).encode() + b"\r\n\r\n"
            )
            assert conn.recv(64).startswith(b"HTTP/1.1 413")
    finally:
        server.should_exit = True
        thread.join(5)


def test_default_cap_is_64_mib() -> None:
    assert get_settings().max_request_bytes == 64 * 1024 * 1024


def test_zero_cap_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(max_request_bytes=0)
