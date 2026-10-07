"""Canonical JSON encoding rules."""

import json
import math
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.canonical_json import (
    MAX_DEPTH,
    CanonicalDepthError,
    CanonicalEncodingError,
    canonical_dumps,
    canonical_loads,
)


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
        """Preserve the JSON type distinction even though Python considers True equal to 1."""
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
        """Encode the same instant identically across different timezone offsets."""
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


def _nested(depth: int) -> object:
    value: object = 0
    for _ in range(depth):
        value = [value]
    return value


class TestDepth:
    def test_nesting_at_the_limit_is_accepted(self) -> None:
        canonical_dumps(_nested(MAX_DEPTH))

    def test_nesting_past_the_limit_is_rejected(self) -> None:
        with pytest.raises(CanonicalDepthError):
            canonical_dumps(_nested(MAX_DEPTH + 1))

    def test_dict_nesting_counts_toward_the_limit(self) -> None:
        value: object = 0
        for _ in range(MAX_DEPTH + 1):
            value = {"a": value}
        with pytest.raises(CanonicalDepthError):
            canonical_dumps(value)

    def test_set_nesting_counts_toward_the_limit(self) -> None:
        value: object = 0
        for _ in range(MAX_DEPTH + 1):
            value = frozenset({value})
        with pytest.raises(CanonicalDepthError):
            canonical_dumps(value)

    def test_encoding_errors_are_value_errors(self) -> None:
        assert issubclass(CanonicalDepthError, CanonicalEncodingError)
        assert issubclass(CanonicalEncodingError, ValueError)
        with pytest.raises(CanonicalEncodingError):
            canonical_dumps(float("nan"))


_WRAP = {
    "dict": lambda v: {"a": v},
    "list": lambda v: [v],
    "tuple": lambda v: (v,),
    "frozenset": lambda v: frozenset({v}),
}
_EMPTY = {"dict": {}, "list": [], "tuple": (), "frozenset": frozenset()}


def _containers(kinds: list[str], *, empty_leaf: bool) -> object:
    """Nest one container per entry in ``kinds``, outermost first.

    With ``empty_leaf`` the innermost container is empty; otherwise it holds a
    scalar. Either way the value has exactly ``len(kinds)`` container levels.
    """
    if empty_leaf:
        value = _EMPTY[kinds[-1]]
        inner = kinds[:-1]
    else:
        value, inner = 0, kinds
    for kind in reversed(inner):
        value = _WRAP[kind](value)
    return value


class TestDepthCountsContainers:
    """The limit counts container levels, whatever the innermost value is."""

    def test_scalars_and_empty_roots(self) -> None:
        assert canonical_dumps(0) == b"0"
        assert canonical_dumps({}) == b"{}"

    @pytest.mark.parametrize("kind", ["dict", "list", "tuple", "frozenset"])
    @pytest.mark.parametrize("empty_leaf", [True, False])
    def test_limit_applies_to_each_container_kind(self, kind: str, empty_leaf: bool) -> None:
        canonical_dumps(_containers([kind] * MAX_DEPTH, empty_leaf=empty_leaf))
        with pytest.raises(CanonicalDepthError):
            canonical_dumps(_containers([kind] * (MAX_DEPTH + 1), empty_leaf=empty_leaf))

    @pytest.mark.parametrize("empty_leaf", [True, False])
    def test_limit_applies_to_mixed_nesting(self, empty_leaf: bool) -> None:
        def mixed(levels: int) -> list[str]:
            return [("dict", "list", "tuple")[i % 3] for i in range(levels)]

        canonical_dumps(_containers(mixed(MAX_DEPTH), empty_leaf=empty_leaf))
        with pytest.raises(CanonicalDepthError):
            canonical_dumps(_containers(mixed(MAX_DEPTH + 1), empty_leaf=empty_leaf))

    def test_limit_applies_to_a_set_holding_nested_members(self) -> None:
        def outer_set(levels: int) -> list[str]:
            return ["frozenset"] + ["tuple"] * (levels - 1)

        canonical_dumps(_containers(outer_set(MAX_DEPTH), empty_leaf=True))
        with pytest.raises(CanonicalDepthError):
            canonical_dumps(_containers(outer_set(MAX_DEPTH + 1), empty_leaf=True))


class TestRFC8785:
    """Vectors from RFC 8785 (JCS): numbers per ECMAScript, keys by UTF-16 units."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.0, b"0"),
            (-0.0, b"0"),
            (1.0, b"1"),
            (4.50, b"4.5"),
            (2e-3, b"0.002"),
            (1e-7, b"1e-7"),
            (1e-27, b"1e-27"),
            (1e16, b"10000000000000000"),
            (1e21, b"1e+21"),
            (333333333.33333329, b"333333333.3333333"),
            (5e-324, b"5e-324"),
            (1.7976931348623157e308, b"1.7976931348623157e+308"),
            (9007199254740991, b"9007199254740991"),
            (-9007199254740991, b"-9007199254740991"),
        ],
    )
    def test_number_serialization(self, value, expected: bytes) -> None:
        assert canonical_dumps(value) == expected

    def test_integral_float_and_int_hash_identically(self) -> None:
        assert canonical_dumps({"temperature": 1.0}) == canonical_dumps({"temperature": 1})

    def test_keys_sort_by_utf16_code_units(self) -> None:
        """RFC 8785 section 3.2.3's property-sorting example."""
        value = {
            "€": "Euro Sign",
            "\r": "Carriage Return",
            "דּ": "Hebrew Letter Dalet With Dagesh",
            "1": "One",
            "\U0001f600": "Emoji: Grinning Face",
            "\u0080": "Control",
            "ö": "Latin Small Letter O With Diaeresis",
        }
        order = list(json.loads(canonical_dumps(value)))
        assert order == ["\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]

    @pytest.mark.parametrize("value", [2**53, -(2**53), 2**63 - 1])
    def test_integers_outside_the_safe_range_are_rejected(self, value: int) -> None:
        with pytest.raises(CanonicalEncodingError):
            canonical_dumps({"seed": value})

    def test_lone_surrogate_is_rejected(self) -> None:
        with pytest.raises(CanonicalEncodingError):
            canonical_dumps("\ud800")

    def test_non_string_keys_are_rejected(self) -> None:
        with pytest.raises(CanonicalEncodingError):
            canonical_dumps({1: "one"})
