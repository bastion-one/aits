"""Shared router CID parsing: exact-length hex, either case, no whitespace."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.hashing import CHECKSUM_BYTES
from app.config import get_settings
from app.routers.common import cid_from_hex, ensure_aware, optional_cid

# Alphabetic digits so upper/mixed-case variants are not vacuous.
_CID = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
_CID_BYTES = bytes.fromhex(_CID)
_WIDTH = CHECKSUM_BYTES * 2


def test_case_variants_decode_to_identical_bytes() -> None:
    mixed = "".join(c.upper() if i % 2 == 0 else c for i, c in enumerate(_CID))
    assert cid_from_hex(_CID) == _CID_BYTES
    assert cid_from_hex(_CID.upper()) == _CID_BYTES
    assert cid_from_hex(mixed) == _CID_BYTES


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * (_WIDTH - 1),
        "a" * (_WIDTH + 1),
        "g" * _WIDTH,
        "\u00ff" * _WIDTH,
        " " + _CID,
        _CID + " ",
        " ".join(_CID[i : i + 2] for i in range(0, _WIDTH, 2)),
        "\t" + _CID,
        _CID + "\t",
        "\n" + _CID,
        _CID + "\n",
        _CID[:32] + " " + _CID[32:],
    ],
)
def test_rejects_malformed_input_with_http_400(value: str) -> None:
    with pytest.raises(HTTPException) as exc:
        cid_from_hex(value)
    assert exc.value.status_code == 400
    assert exc.value.detail == f"cid is not a 64-character hex CID: {value}"


def test_error_identifies_supplied_field() -> None:
    with pytest.raises(HTTPException) as exc:
        cid_from_hex("", field="expected_head")
    assert exc.value.status_code == 400
    assert "expected_head" in exc.value.detail


def test_optional_cid_none_stays_none() -> None:
    assert optional_cid(None) is None


def test_optional_cid_non_null_uses_the_same_parser() -> None:
    assert optional_cid(_CID.upper()) == _CID_BYTES
    with pytest.raises(HTTPException) as exc:
        optional_cid(" " + _CID)
    assert exc.value.status_code == 400
    assert exc.value.detail == f"cid is not a 64-character hex CID:  {_CID}"


def test_ensure_aware_accepts_the_past_and_small_clock_skew() -> None:
    now = datetime.now(timezone.utc)
    for value in (now - timedelta(days=3650), now, now + timedelta(seconds=60)):
        assert ensure_aware(value, field="occurred_at") == value


def test_ensure_aware_rejects_forward_dated_event_times() -> None:
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    with pytest.raises(HTTPException) as exc:
        ensure_aware(future, field="occurred_at")
    assert exc.value.status_code == 400
    assert "occurred_at" in exc.value.detail


def test_forward_skew_is_configurable(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "occurred_at_max_skew_seconds", 7200)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert ensure_aware(future, field="occurred_at") == future


def test_missing_agent_error_identifies_the_uuid(session):
    from uuid import UUID
    from app.routers.common import get_agent_row

    missing = UUID("00000000-0000-4000-8000-000000000009")
    with pytest.raises(HTTPException) as exc:
        get_agent_row(session, missing)
    assert exc.value.status_code == 404
    assert exc.value.detail == f"Agent not found: {missing}"


def test_forward_skew_boundary_is_inclusive(monkeypatch):
    from app.routers import common

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(common, "datetime", Clock)
    monkeypatch.setattr(get_settings(), "occurred_at_max_skew_seconds", 60)
    assert ensure_aware(None, field="time") == now
    assert ensure_aware(now + timedelta(seconds=60), field="time") == now + timedelta(seconds=60)
    with pytest.raises(HTTPException) as exc:
        ensure_aware(now + timedelta(seconds=60, microseconds=1), field="time")
    assert exc.value.status_code == 400
    assert exc.value.detail == "time is more than 60 seconds ahead of the server clock"


def test_date_dependent_timezone_is_aware():
    from zoneinfo import ZoneInfo

    value = datetime(2026, 1, 1, tzinfo=ZoneInfo("America/Chicago"))
    assert ensure_aware(value, field="time") == value
