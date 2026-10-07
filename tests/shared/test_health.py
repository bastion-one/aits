"""The /health endpoint: a concrete, typed response model.

Guards against regressing to a free-form ``dict[str, str]`` return, which
FastAPI serializes as ``additionalProperties`` -- the generated SDK renders
that map value as ``Optional[str]``, a nested type its deserializer cannot
resolve, so ``HealthApi.health()`` crashes on an otherwise-correct 200.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlmodel import Session, create_engine

from app.db import get_session
from app.main import app


def test_health_returns_typed_status(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_schema_is_a_named_component(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    health_200 = schema["paths"]["/health"]["get"]["responses"]["200"]
    body = health_200["content"]["application/json"]["schema"]
    # A $ref to a named model, not an inline additionalProperties map.
    assert body == {"$ref": "#/components/schemas/HealthStatus"}
    assert "additionalProperties" not in str(body)


def test_ready_reports_a_reachable_database(client: TestClient) -> None:
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_ready_is_a_503_when_the_database_is_unreachable(client: TestClient) -> None:
    unreachable = create_engine("sqlite:////nonexistent-dir/aits.db")

    def _override() -> Iterator[Session]:
        with Session(unreachable) as session:
            yield session

    app.dependency_overrides[get_session] = _override
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert client.get("/health").status_code == 200, "liveness does not touch the database"


def test_ready_is_a_503_when_the_pool_is_exhausted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _exhausted(self: Session) -> None:
        raise PoolTimeoutError("QueuePool limit reached")

    monkeypatch.setattr(Session, "connection", _exhausted)
    assert client.get("/ready").status_code == 503
