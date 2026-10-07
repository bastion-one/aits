"""Runtime configuration loaded from environment / .env via pydantic-settings.

Recommended FastAPI pattern: a single :class:`Settings` instance, cached so
that env parsing happens once per process. Settings consumers either depend on
``get_settings()`` (handlers, dependencies) or import the cached instance
directly (module-level wiring such as the SQLAlchemy engine).

Override by exporting environment variables or by populating
``.env`` / ``.env.local`` next to the project root. ``.env.local`` wins.
"""

import re
from functools import cached_property, lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SERVICE_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")


def parse_service_keys(raw: str) -> dict[str, str]:
    """Parse ``name:secret`` pairs into ``{secret: name}``.

    Raises ``ValueError`` for a malformed entry, a duplicate name, or a
    duplicate secret, so a typo fails at startup instead of locking a
    service out.
    """
    keys: dict[str, str] = {}
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        name, sep, secret = entry.partition(":")
        if not sep or not secret or not _SERVICE_NAME.fullmatch(name):
            raise ValueError(
                "each AUTH_SERVICE_KEYS entry must be name:secret, with a name of "
                "letters, digits, '.', '_', or '-'"
            )
        if name in keys.values() or secret in keys:
            raise ValueError(f"AUTH_SERVICE_KEYS repeats the name or secret for {name!r}")
        keys[secret] = name
    return keys


class Settings(BaseSettings):
    """Process-wide config; instantiate via :func:`get_settings`."""

    database_url: str = Field(
        default="postgresql+psycopg://bastion:bastion@127.0.0.1:5432/bastion",
        description=(
            "SQLAlchemy URL. Default targets the docker-compose postgres. "
            "Tests build their own in-memory sqlite engine and bypass this."
        ),
    )

    commit_lock_timeout_ms: int = Field(
        default=5000,
        ge=1,
        description=(
            "How long a writer waits for the PostgreSQL commit lock before the "
            "request fails with 503 and Retry-After."
        ),
    )

    occurred_at_max_skew_seconds: int = Field(
        default=300,
        ge=0,
        description=(
            "How far ahead of server time a self-asserted occurred_at may be. Past "
            "times are accepted, so saved records can be replayed."
        ),
    )

    max_request_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=1,
        description="Largest accepted request body, including artifact uploads (413 above).",
    )

    auth_service_keys: str = Field(
        default="",
        description="Comma-separated name:secret service keys for the API (app.auth).",
    )
    auth_disabled: bool = Field(
        default=False,
        description="Accept unauthenticated requests. For local development only.",
    )

    @field_validator("auth_service_keys")
    @classmethod
    def _check_service_keys(cls, raw: str) -> str:
        parse_service_keys(raw)
        return raw

    @cached_property
    def service_keys(self) -> dict[str, str]:
        """``auth_service_keys`` parsed into ``{secret: name}``, once per instance."""
        return parse_service_keys(self.auth_service_keys)

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
