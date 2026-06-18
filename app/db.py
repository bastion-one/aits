"""Database engine + ``SessionDep`` FastAPI dependency.

The aware-UTC contract for ``created_at`` is enforced by
:class:`app.types.UTCDateTime` (a SQLAlchemy ``TypeDecorator``), not by
driver-specific adapters. As a result this module is now dialect-agnostic:
swap ``DATABASE_URL`` between sqlite (dev/local) and postgres (container) and
the rest of the app is unaffected.
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings


def _build_engine(database_url: str) -> Engine:
    """Build the SQLAlchemy engine, applying sqlite-only quirks where required.

    ``pool_pre_ping`` tests each pooled connection at checkout, so a restarted
    database (``make db-reset``/``db-up`` under a long-running server) costs a
    transparent reconnect instead of one failed request per stale connection.
    """

    connect_args: dict[str, object] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)


engine: Engine = _build_engine(get_settings().database_url)


def init_db() -> None:
    """Create all tables. Idempotent.

    Schema evolution past this point should land via a proper migration tool
    (Alembic) rather than relying on ``create_all`` against a long-lived DB.
    """
    # create_all only sees tables whose model classes are imported; pull them
    # in here so init_db works standalone (scripts, shells), not just under
    # the full app import.
    from . import models  # pylint: disable=import-outside-toplevel,unused-import

    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
