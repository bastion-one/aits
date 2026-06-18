"""Runtime configuration loaded from environment / .env via pydantic-settings.

Recommended FastAPI pattern: a single :class:`Settings` instance, cached so
that env parsing happens once per process. Settings consumers either depend on
``get_settings()`` (handlers, dependencies) or import the cached instance
directly (module-level wiring such as the SQLAlchemy engine).

Override by exporting environment variables or by populating
``.env`` / ``.env.local`` next to the project root. ``.env.local`` wins.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide config; instantiate via :func:`get_settings`."""

    database_url: str = Field(
        default="postgresql+psycopg://bastion:bastion@127.0.0.1:5432/bastion",
        description=(
            "SQLAlchemy URL. Default targets the docker-compose postgres. "
            "Tests build their own in-memory sqlite engine and bypass this."
        ),
    )

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
