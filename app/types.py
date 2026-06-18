"""Custom SQLAlchemy column types.

`UTCDateTime` is the single source of truth for the project's datetime
contract: aware UTC on the way in, aware UTC on the way out, microsecond
precision preserved across sqlite and postgres. The contract is enforced in
Python so that ``app.canonical_json`` and ``app.validation`` can treat
``row.created_at`` as a byte-stable, aware-UTC ``datetime`` regardless of the
underlying backend.

On sqlite the column is TEXT (ISO 8601 with explicit ``+00:00`` and 6-digit
microseconds). On postgres it is ``TIMESTAMPTZ``.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import TIMESTAMP as PG_TIMESTAMP
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Aware-UTC datetime column, byte-stable across sqlite and postgres."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String())
        return dialect.type_descriptor(PG_TIMESTAMP(timezone=True))

    def process_bind_param(  # type: ignore[override]
        self, value: datetime | None, dialect: Dialect
    ) -> str | datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UTCDateTime expected datetime, got {type(value).__name__}")
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(f"naive datetime not allowed for storage: {value!r}")
        utc = value.astimezone(timezone.utc)
        if dialect.name == "sqlite":
            return utc.isoformat(timespec="microseconds")
        return utc

    def process_result_value(  # type: ignore[override]
        self, value: str | datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
