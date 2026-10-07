"""Settings validation."""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_commit_lock_timeout_must_be_positive() -> None:
    """0 would mean an unbounded wait in Postgres, and negatives are rejected by it."""
    with pytest.raises(ValidationError):
        Settings(commit_lock_timeout_ms=0)


def test_negative_occurred_at_skew_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(occurred_at_max_skew_seconds=-1)
