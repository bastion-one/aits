"""Canonical JSON encoding (RFC 8785-ish) for tamper-evident hashing.

Rules:
- dict keys sorted lexicographically; no whitespace separators.
- list/tuple encoded as JSON arrays; order preserved.
- set/frozenset encoded as a JSON array whose elements are sorted by each
  element's own canonical bytes -- an order-independent, deduped collection.
  Use this (rather than a list) to express "order must not affect the hash";
  equal sets hash identically regardless of insertion order. Members must be
  hashable.
- str UTF-8 encoded; no Unicode normalization.
- int as JSON number.
- float: finite only; ``-0.0`` -> ``0.0``; NaN/Inf raise ``ValueError``.
- bool as JSON true/false (distinguished from int).
- None as JSON null.
- bytes as standard base64 (padded) wrapped in a JSON string.
- datetime: aware only; normalized to UTC; ISO-8601 with explicit ``Z`` suffix.
- UUID: canonical lowercase hyphenated string form.

Anything else raises ``TypeError``.
"""

import base64
import json
import math
from datetime import datetime, timezone
from typing import Any
from uuid import UUID


class _CanonicalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, bytes):
            return base64.b64encode(o).decode("ascii")
        if isinstance(o, datetime):
            if o.tzinfo is None or o.tzinfo.utcoffset(o) is None:
                raise ValueError(f"naive datetime not allowed in canonical encoding: {o!r}")
            iso = o.astimezone(timezone.utc).isoformat()
            assert iso.endswith("+00:00"), f"unexpected isoformat tail: {iso!r}"
            return iso[: -len("+00:00")] + "Z"
        if isinstance(o, UUID):
            return str(o)
        return super().default(o)


def _normalize(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"non-finite float not allowed in canonical encoding: {value!r}")
        if value == 0.0:
            return 0.0
        return value
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_normalize(v) for v in value)
    if isinstance(value, (set, frozenset)):
        # Sorting each element by its own canonical bytes is a deterministic
        # total order, so equal sets serialize identically regardless of
        # insertion order. Set members are always hashable (bytes/str/int/UUID/
        # bool/None/frozenset/tuple), so the heterogeneous-dict sort problem
        # cannot arise.
        return sorted((_normalize(v) for v in value), key=canonical_dumps)
    return value


def canonical_dumps(value: Any) -> bytes:
    """Encode a Python value to canonical JSON bytes (UTF-8)."""
    return json.dumps(
        _normalize(value),
        cls=_CanonicalEncoder,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_loads(data: bytes) -> Any:
    """Decode canonical JSON bytes back to a Python value.

    Debug aid only -- lossy for ``bytes`` / ``datetime`` / ``UUID``, which
    round-trip as their string-encoded representations.
    """
    return json.loads(data.decode("utf-8"))
