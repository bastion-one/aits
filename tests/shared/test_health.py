"""The /health endpoint: a concrete, typed response model.

Guards against regressing to a free-form ``dict[str, str]`` return, which
FastAPI serializes as ``additionalProperties`` -- the generated SDK renders
that map value as ``Optional[str]``, a nested type its deserializer cannot
resolve, so ``HealthApi.health()`` crashes on an otherwise-correct 200.
"""

from fastapi.testclient import TestClient


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
