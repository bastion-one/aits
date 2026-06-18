"""SHA-256 primitives used for content addressing and chain integrity."""

import hashlib

CHECKSUM_BYTES = 32

ZERO_PREV: bytes = b"\x00" * CHECKSUM_BYTES
"""Sentinel ``prev_checksum`` for the head of a chained-record history."""


def sha256_hash(data: bytes) -> bytes:
    """Return the SHA-256 digest of ``data`` as raw bytes (length 32)."""
    return hashlib.sha256(data).digest()
