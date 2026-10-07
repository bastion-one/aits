"""Helpers shared across the resource routers."""

import re
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field
from sqlmodel import Session, select

from ..config import get_settings
from ..hashing import CHECKSUM_BYTES
from ..models import Agent

MAX_TEXT = 1_000_000
"""Longest free-text field (prompts, inputs, outputs), in characters."""
MAX_LABEL = 2048
"""Longest name, identifier, or label, in characters."""
MAX_ITEMS = 1000
"""Most items in a request list."""

LongText = Annotated[str, Field(max_length=MAX_TEXT)]
Label = Annotated[str, Field(max_length=MAX_LABEL)]

_CID_HEX = re.compile(rf"[0-9a-fA-F]{{{CHECKSUM_BYTES * 2}}}")


def get_agent_row(session: Session, agent_uuid: UUID) -> Agent:
    row = session.exec(select(Agent).where(Agent.uuid == agent_uuid)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_uuid}")
    return row


def cid_from_hex(value: str, *, field: str = "cid") -> bytes:
    """Accept exactly ``CHECKSUM_BYTES * 2`` ASCII hex characters of either case.

    Rejects whitespace and non-hexadecimal characters with HTTP 400.
    Returns decoded bytes. Serialize with ``.hex()`` for lowercase output.
    """
    if not _CID_HEX.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail=f"{field} is not a 64-character hex CID: {value}",
        )
    return bytes.fromhex(value)


def optional_cid(value: str | None, *, field: str = "cid") -> bytes | None:
    return None if value is None else cid_from_hex(value, field=field)


def ensure_aware(value: datetime | None, *, field: str) -> datetime:
    """Default a missing event time to server now; reject naive and
    forward-dated datetimes.

    The event time is self-asserted and sealed inside the CID, and traceback
    orders by it, so a future timestamp would reorder the apparent timeline
    and still verify. Times more than ``occurred_at_max_skew_seconds`` ahead
    of the server clock are rejected. Past times are accepted: replayed
    records are legitimately late, and ``recorded_at`` shows the delay.
    """
    now = datetime.now(timezone.utc)
    if value is None:
        return now
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise HTTPException(status_code=400, detail=f"{field} must be an aware datetime")
    skew = get_settings().occurred_at_max_skew_seconds
    if value > now + timedelta(seconds=skew):
        raise HTTPException(
            status_code=400,
            detail=f"{field} is more than {skew} seconds ahead of the server clock",
        )
    return value
