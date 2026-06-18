"""Helpers shared across the resource routers."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlmodel import Session, select

from ..hashing import CHECKSUM_BYTES
from ..models import Agent


def get_agent_row(session: Session, agent_uuid: UUID) -> Agent:
    row = session.exec(select(Agent).where(Agent.uuid == agent_uuid)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_uuid}")
    return row


def cid_from_hex(value: str, *, field: str = "cid") -> bytes:
    try:
        cid = bytes.fromhex(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be hex: {exc}") from exc
    if len(cid) != CHECKSUM_BYTES:
        raise HTTPException(
            status_code=400, detail=f"{field} must be {CHECKSUM_BYTES} bytes of hex"
        )
    return cid


def optional_cid(value: str | None, *, field: str = "cid") -> bytes | None:
    return None if value is None else cid_from_hex(value, field=field)


def ensure_aware(value: datetime | None, *, field: str) -> datetime:
    """Default a missing event time to server now; reject naive datetimes."""
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise HTTPException(status_code=400, detail=f"{field} must be an aware datetime")
    return value
