"""UTC storage conversions, including driver values and invalid inputs."""

from datetime import datetime, timedelta, timezone, tzinfo

import pytest
from sqlalchemy.dialects import postgresql, sqlite

from app.types import UTCDateTime


class NoOffset(tzinfo):
    def utcoffset(self, dt):
        return None


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_null_datetime(dialect):
    column = UTCDateTime()
    assert column.process_bind_param(None, dialect) is None
    assert column.process_result_value(None, dialect) is None


@pytest.mark.parametrize("value", ["2026-01-01", 123])
def test_reject_non_datetime(value):
    with pytest.raises(TypeError, match="UTCDateTime expected datetime"):
        UTCDateTime().process_bind_param(value, sqlite.dialect())


@pytest.mark.parametrize("zone", [None, NoOffset()])
def test_reject_naive_datetime(zone):
    with pytest.raises(ValueError, match="naive datetime not allowed for storage"):
        UTCDateTime().process_bind_param(datetime(2026, 1, 1, tzinfo=zone), sqlite.dialect())


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_offset_datetime_preserves_microseconds(dialect):
    value = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone(timedelta(hours=5)))
    expected = datetime(2026, 1, 1, 22, 4, 5, 678901, tzinfo=timezone.utc)
    column = UTCDateTime()
    bound = column.process_bind_param(value, dialect)
    assert bound == (
        expected.isoformat(timespec="microseconds") if dialect.name == "sqlite" else expected
    )
    result = column.process_result_value(bound, dialect)
    assert result == expected
    assert result.tzinfo is timezone.utc


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01T00:00:00.000123",
        datetime(2026, 1, 1, microsecond=123),
        datetime(2026, 1, 1, microsecond=123, tzinfo=NoOffset()),
    ],
)
def test_naive_driver_result_is_utc(value):
    result = UTCDateTime().process_result_value(value, sqlite.dialect())
    assert result == datetime(2026, 1, 1, microsecond=123, tzinfo=timezone.utc)
    assert result.tzinfo is timezone.utc
