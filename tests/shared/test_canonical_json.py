"""Canonical JSON encoding rules."""

import math
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.canonical_json import canonical_dumps, canonical_loads


class TestSortedKeys:
    def test_dict_keys_are_sorted_lexicographically(self) -> None:
        a = canonical_dumps({"b": 1, "a": 2, "c": 3})
        assert a == b'{"a":2,"b":1,"c":3}'

    def test_insertion_order_does_not_change_bytes(self) -> None:
        a = canonical_dumps({"x": 1, "y": 2})
        b = canonical_dumps({"y": 2, "x": 1})
        assert a == b


class TestSeparators:
    def test_no_whitespace_between_items(self) -> None:
        assert canonical_dumps({"a": 1, "b": 2}) == b'{"a":1,"b":2}'
        assert canonical_dumps([1, 2, 3]) == b"[1,2,3]"


class TestFloats:
    def test_negative_zero_normalizes_to_positive_zero(self) -> None:
        assert canonical_dumps(-0.0) == canonical_dumps(0.0)

    def test_nan_rejected(self) -> None:
        with pytest.raises(ValueError):
            canonical_dumps(float("nan"))

    def test_inf_rejected(self) -> None:
        with pytest.raises(ValueError):
            canonical_dumps(math.inf)

    def test_neg_inf_rejected(self) -> None:
        with pytest.raises(ValueError):
            canonical_dumps(-math.inf)


class TestBoolVsInt:
    def test_true_is_not_one(self) -> None:
        assert canonical_dumps(True) != canonical_dumps(1)
        assert canonical_dumps(True) == b"true"
        assert canonical_dumps(1) == b"1"


class TestBytes:
    def test_bytes_encoded_as_base64_string(self) -> None:
        assert canonical_dumps(b"\x00\x01\x02") == b'"AAEC"'


class TestDatetime:
    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError):
            canonical_dumps(datetime(2026, 1, 1, 12, 0, 0))

    def test_aware_datetime_uses_z_suffix(self) -> None:
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert canonical_dumps(dt) == b'"2026-01-01T12:00:00Z"'

    def test_non_utc_normalized_to_utc(self) -> None:
        eastern = timezone(timedelta(hours=-5))
        dt_eastern = datetime(2026, 1, 1, 7, 0, 0, tzinfo=eastern)
        dt_utc = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert canonical_dumps(dt_eastern) == canonical_dumps(dt_utc)


class TestUUID:
    def test_uuid_lowercase_hyphenated(self) -> None:
        u = UUID("12345678-1234-5678-1234-567812345678")
        assert canonical_dumps(u) == b'"12345678-1234-5678-1234-567812345678"'


class TestSet:
    def test_set_encoding_is_order_independent(self) -> None:
        assert canonical_dumps({"a", "b", "c"}) == canonical_dumps({"c", "a", "b"})

    def test_set_is_byte_stable(self) -> None:
        assert canonical_dumps({3, 1, 2}) == b"[1,2,3]"

    def test_frozenset_matches_equivalent_set(self) -> None:
        assert canonical_dumps(frozenset({"y", "x"})) == canonical_dumps({"x", "y"})

    def test_set_of_bytes_is_order_independent(self) -> None:
        a, b = b"\x01\x02", b"\xff\x00"
        assert canonical_dumps({a, b}) == canonical_dumps({b, a})

    def test_list_order_still_matters(self) -> None:
        # Regression guard: we did NOT globally sort sequences. Lists with the
        # same members in a different order must still hash differently.
        assert canonical_dumps([1, 2, 3]) != canonical_dumps([3, 2, 1])


class TestRoundTrip:
    def test_json_native_round_trip(self) -> None:
        value = {"a": [1, 2.5, "s", True, None], "b": {"nested": 0.0}}
        assert canonical_loads(canonical_dumps(value)) == value


class TestRejectUnsupported:
    def test_unsupported_type_raises(self) -> None:
        class C:
            pass

        with pytest.raises(TypeError):
            canonical_dumps(C())
