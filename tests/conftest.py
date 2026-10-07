"""Shared test fixtures: isolated in-memory SQLite + FastAPI TestClient.

The unit tests are intentionally backend-agnostic: they hit the FastAPI app
through TestClient, override ``get_session`` to point at an in-memory sqlite
engine, and let the :class:`app.types.UTCDateTime` ``TypeDecorator`` handle
the aware-UTC contract. No sqlite-stdlib adapter wiring needed.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import db, ledger
from app.config import get_settings
from app.db import get_session
from app.main import app


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        ledger.init_ledger(s)
    yield eng
    SQLModel.metadata.drop_all(eng)


@pytest.fixture
def session(engine) -> Iterator[Session]:
    with Session(engine) as s:
        ledger.attest(s, "test")
        yield s


@pytest.fixture(autouse=True)
def open_auth(monkeypatch) -> None:
    """Every test runs the API in open mode, including tests that build their
    own ``TestClient(app)``; ``tests/shared/test_auth.py`` turns keys back on.

    ``Settings.service_keys`` is a cached property, so drop any cached value:
    a test that sets ``auth_service_keys`` then gets that string parsed."""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_disabled", True)
    settings.__dict__.pop("service_keys", None)


@pytest.fixture
def client(session, engine) -> Iterator[TestClient]:
    """Yields a TestClient with ``get_session`` overridden to the in-memory engine.

    Note: ``TestClient`` is intentionally *not* used as a context manager so
    that the app's lifespan (which calls :func:`app.db.init_db` against the
    real configured engine) does not run. The conftest creates schema itself
    on the in-memory engine via the ``engine`` fixture.
    """

    def _override() -> Iterator[Session]:
        yield session

    app.dependency_overrides[db.get_engine] = lambda: engine
    app.dependency_overrides[get_session] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
