"""Integration-suite fixtures: spawned uvicorn + ephemeral sqlite.

Each session boots a fresh ``uvicorn app.main:app`` subprocess pointed at an
ephemeral sqlite tempfile via ``DATABASE_URL``. The subprocess builds its own
``app.config.Settings`` from the env, so the engine is created against the
tempfile and the lifespan ``init_db()`` materializes the schema before the
first request.

Tests in this directory hit the running server through the generated
``aits-client`` SDK, exercising the full HTTP -> ASGI -> SQLAlchemy
path. The SDK must already be built (``make regen``); if it isn't,
``pytest.importorskip`` skips the suite with an actionable message.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

aits_client = pytest.importorskip(
    "aits_client",
    reason="aits-client SDK is not installed; run `make regen` first",
)

SERVICE_KEY = secrets.token_urlsafe(32)
"""The spawned server's only service key; the SDK presents it as a Bearer token."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=1) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
        time.sleep(0.1)
    raise RuntimeError(f"server at {url} did not become healthy in {timeout}s ({last_exc})")


@pytest.fixture(scope="session")
def base_url() -> Iterator[str]:
    repo_root = Path(__file__).resolve().parents[2]
    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(prefix="bastion-integ-") as db_dir:
        db_path = Path(db_dir) / "test.db"
        env = {
            **os.environ,
            "DATABASE_URL": f"sqlite:///{db_path}",
            "AUTH_SERVICE_KEYS": f"integration:{SERVICE_KEY}",
            "AUTH_DISABLED": "false",
        }
        # Not a `with`: the process must outlive this scope until `yield`, and
        # the finally below does a proper terminate/kill/wait that Popen's own
        # __exit__ (which only waits) would not.
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            try:
                _wait_for_health(url)
            except Exception as exc:
                # Surface the subprocess output so the user can see *why* it died.
                proc.terminate()
                try:
                    out, _ = proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, _ = proc.communicate()
                raise RuntimeError(
                    f"server failed to become healthy: {exc}\n"
                    f"--- subprocess output ---\n{out.decode(errors='replace')}"
                ) from exc
            yield url
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def api_client(base_url: str) -> Iterator[aits_client.ApiClient]:
    cfg = aits_client.Configuration(host=base_url, access_token=SERVICE_KEY)
    with aits_client.ApiClient(cfg) as ac:
        yield ac
