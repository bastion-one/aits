"""Canonical JSON encoding for tamper-evident hashing.

Encoding is two steps. First, AITS values are projected onto plain JSON:

- bytes: standard base64 (padded) in a JSON string.
- datetime: aware only; normalized to UTC; ISO-8601 with a ``Z`` suffix
  (microseconds appear only when non-zero).
- UUID: canonical lowercase hyphenated string.
- set/frozenset: a JSON array sorted by each member's own canonical bytes, so
  equal sets encode identically regardless of insertion order.
- tuple: a JSON array, order preserved.
- containers may nest at most ``MAX_DEPTH`` levels.

Second, the JSON value is serialized per RFC 8785 (JSON Canonicalization
Scheme): no whitespace, object keys sorted by UTF-16 code units, strings as
UTF-8 with minimal escaping, and numbers in ECMAScript form (``1.0`` is
``1``, ``1e16`` is ``10000000000000000``). RFC 8785 inherits I-JSON's
limits: integers must lie within +/-(2**53 - 1), floats must be finite, keys
must be strings, and strings must be valid Unicode. Values outside those
limits raise ``CanonicalEncodingError``, which the API reports as 400.

Any other type raises ``TypeError``: a programming error, not bad input.

An independent verifier reproduces a CID with any RFC 8785 implementation
plus the projection above.
"""

import base64
import json
import math
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import rfc8785

MAX_DEPTH = 64
"""Maximum container nesting depth accepted for hashing.

The count includes the canonical encoding's own two wrapper levels (the
``[content, links]`` pair and the content dict), so user metadata may nest
``MAX_DEPTH - 2`` = 62 levels deep.
"""


class CanonicalEncodingError(ValueError):
    """The value cannot be canonically encoded, so it cannot be hashed."""


class CanonicalDepthError(CanonicalEncodingError):
    """The value nests deeper than ``MAX_DEPTH``."""


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise CanonicalEncodingError(f"naive datetime not allowed in canonical encoding: {value!r}")
    iso = value.astimezone(timezone.utc).isoformat()
    assert iso.endswith("+00:00"), f"unexpected isoformat tail: {iso!r}"
    return iso[: -len("+00:00")] + "Z"


def _project_scalar(value: Any) -> Any:
    """Project a non-container AITS value onto a JSON scalar."""
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, datetime):
        return _utc_iso(value)
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, (str, int)):
        return value
    raise TypeError(f"type not supported by canonical encoding: {type(value).__name__}")


def _normalize(value: Any, depth: int = 0) -> Any:
    """Normalize ``value``; ``depth`` counts the containers enclosing it.

    A container counts when it is entered, so a scalar has zero levels and an
    empty root container has one. At most ``MAX_DEPTH`` levels are accepted,
    whether or not the innermost container is empty.
    """
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        depth += 1
        if depth > MAX_DEPTH:
            raise CanonicalDepthError(f"value nests deeper than {MAX_DEPTH} levels")
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise CanonicalEncodingError(
                f"non-finite float not allowed in canonical encoding: {value!r}"
            )
        return value
    if isinstance(value, dict):
        return {k: _normalize(v, depth) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v, depth) for v in value]
    if isinstance(value, tuple):
        return [_normalize(v, depth) for v in value]
    if isinstance(value, (set, frozenset)):
        # Sorting each element by its own canonical bytes is a deterministic
        # total order, so equal sets serialize identically regardless of
        # insertion order. Set members are always hashable (bytes/str/int/UUID/
        # bool/None/frozenset/tuple), so the heterogeneous-dict sort problem
        # cannot arise.
        return sorted((_normalize(v, depth) for v in value), key=canonical_dumps)
    return _project_scalar(value)


def canonical_dumps(value: Any) -> bytes:
    """Encode a Python value to canonical JSON bytes (RFC 8785, UTF-8)."""
    try:
        return rfc8785.dumps(_normalize(value))
    except rfc8785.CanonicalizationError as exc:
        raise CanonicalEncodingError(f"value cannot be canonically encoded: {exc}") from exc


def canonical_loads(data: bytes) -> Any:
    """Decode canonical JSON bytes back to a Python value.

    Debug aid only -- lossy for ``bytes`` / ``datetime`` / ``UUID``, which
    round-trip as their string-encoded representations.
    """
    return json.loads(data.decode("utf-8"))
