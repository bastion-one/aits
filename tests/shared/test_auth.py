"""Service-key authentication: fails closed, Bearer keys, open health checks."""

import pytest
from fastapi.testclient import TestClient

from app.auth import ensure_auth_configured
from app.config import Settings, get_settings, parse_service_keys
from app.main import app

KEYS = "gateway:s3cret-one,recorder:s3cret:two"


@pytest.fixture
def keyed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_disabled", False)
    monkeypatch.setattr(settings, "auth_service_keys", KEYS)
    return client


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def test_parse_service_keys_maps_secrets_to_names() -> None:
    assert parse_service_keys(KEYS) == {"s3cret-one": "gateway", "s3cret:two": "recorder"}
    assert not parse_service_keys(" , ")


@pytest.mark.parametrize(
    "raw",
    ["no-colon", "bad name:secret", ":secret", "name:", "a:x,a:y", "a:x,b:x"],
)
def test_parse_service_keys_rejects_malformed_entries(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_service_keys(raw)


def test_startup_requires_keys_or_explicit_open_mode() -> None:
    with pytest.raises(RuntimeError, match="AUTH_SERVICE_KEYS"):
        ensure_auth_configured(Settings(auth_service_keys="", auth_disabled=False))
    with pytest.raises(ValueError, match="name:secret"):
        Settings(auth_service_keys="oops", auth_disabled=False)
    ensure_auth_configured(Settings(auth_service_keys=KEYS, auth_disabled=False))
    ensure_auth_configured(Settings(auth_service_keys="", auth_disabled=True))


def test_missing_key_is_a_401_with_a_bearer_challenge(keyed: TestClient) -> None:
    r = keyed.get("/agents/")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_wrong_key_is_a_401(keyed: TestClient) -> None:
    assert keyed.get("/agents/", headers=_bearer("nope")).status_code == 401
    assert keyed.get("/agents/", headers=_bearer("s3cret-on")).status_code == 401
    non_ascii = {"Authorization": "Bearer s3cret-\u00fcne".encode("latin-1")}
    assert keyed.get("/agents/", headers=non_ascii).status_code == 401


def test_every_configured_key_reads_and_writes(keyed: TestClient) -> None:
    for secret in ("s3cret-one", "s3cret:two"):
        assert keyed.get("/agents/", headers=_bearer(secret)).status_code == 200
        r = keyed.post("/agents/", json={"name": "svc"}, headers=_bearer(secret))
        assert r.status_code == 201


def test_health_and_openapi_stay_open(keyed: TestClient) -> None:
    assert keyed.get("/health").status_code == 200
    assert keyed.get("/openapi.json").status_code == 200


def test_empty_keys_with_open_mode_off_fail_closed(client: TestClient, monkeypatch) -> None:
    """Startup refuses this configuration; if it is reached anyway, nothing is let in."""
    monkeypatch.setattr(get_settings(), "auth_disabled", False)
    monkeypatch.setattr(get_settings(), "auth_service_keys", "")
    assert client.get("/agents/").status_code == 401


def test_openapi_declares_bearer_security_on_resource_routes() -> None:
    schema = app.openapi()
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert schema["paths"]["/agents/"]["get"]["security"] == [{"HTTPBearer": []}]
    assert "security" not in schema["paths"]["/health"]["get"]
