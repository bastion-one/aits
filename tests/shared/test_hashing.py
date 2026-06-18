"""SHA-256 primitives."""

import hashlib

from app.hashing import CHECKSUM_BYTES, ZERO_PREV, sha256_hash


def test_sha256_returns_32_bytes() -> None:
    digest = sha256_hash(b"hello")
    assert isinstance(digest, bytes)
    assert len(digest) == 32
    assert digest == hashlib.sha256(b"hello").digest()


def test_zero_prev_is_32_zero_bytes() -> None:
    assert ZERO_PREV == b"\x00" * CHECKSUM_BYTES
    assert len(ZERO_PREV) == 32
