"""Endpoints for ``Artifact`` nodes (content-addressed byte objects) and aliases.

Ingestion is **raw bytes**: ``POST /artifacts/`` receives the object bytes
(``application/octet-stream``), hashes them, and commits the artifact node
``{type, sha256}`` -- the bytes themselves are dropped after hashing. An
artifact answers to both of its hashes: ``GET /artifacts/by-digest/`` looks up
by the raw byte digest (re-hash a file, ask "have we seen these bytes"),
while the CID is the node identity DUTs link in the DAG. The mutable
``locator`` points to wherever the data lives and is outside the CID.
Aliases record the upload-once / reference-many file ids inference providers
issue (e.g. an Anthropic ``file_...`` id) against a known artifact, enabling
later ``resolve`` lookups.

``POST /artifacts/upload/`` is a multipart convenience wrapper: it hashes an
uploaded file and registers an alias (defaulting to the filename) in one call.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import SessionDep
from ..hashing import sha256_hash, sha256_hasher
from ..ledger import record
from ..models import Artifact, ArtifactAlias
from .common import MAX_LABEL, Label, cid_from_hex

router = APIRouter(tags=["artifacts"])

# Default ``source`` stamped on the alias when a file is uploaded via the
# multipart convenience endpoint without an explicit source.
_DEFAULT_UPLOAD_SOURCE = "upload"


class AliasRead(BaseModel):
    source: str
    alias: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: ArtifactAlias) -> "AliasRead":
        return cls(source=row.source, alias=row.alias, created_at=row.created_at)


class AliasCreate(BaseModel):
    source: Label
    alias: Label


class ArtifactRead(BaseModel):
    cid: str
    sha256: str
    locator: str | None
    aliases: list[AliasRead]

    @classmethod
    def from_row(cls, row: Artifact, aliases: list[ArtifactAlias]) -> "ArtifactRead":
        return cls(
            cid=row.cid.hex(),
            sha256=row.sha256.hex(),
            locator=row.locator,
            aliases=[AliasRead.from_row(a) for a in aliases],
        )


class ArtifactUpdate(BaseModel):
    locator: Label | None


class AliasResolveRead(BaseModel):
    cid: str
    source: str
    alias: str


def _aliases_for(session: Session, cid: bytes) -> list[ArtifactAlias]:
    return list(
        session.exec(
            select(ArtifactAlias)
            .where(ArtifactAlias.artifact_cid == cid)
            .order_by(ArtifactAlias.id)
        ).all()
    )


def _ingest(session: Session, body: bytes) -> Artifact:
    stored, _ = record(session, Artifact(sha256=sha256_hash(body)))
    return stored


def _upsert_alias(session: Session, cid: bytes, source: str, alias: str) -> ArtifactAlias:
    """Idempotent on ``(source, alias)``; 409 if the pair maps to another artifact.

    Assumes the artifact exists -- callers reaching this from a hex path id must
    404-check first.
    """
    existing = session.exec(
        select(ArtifactAlias)
        .where(ArtifactAlias.source == source)
        .where(ArtifactAlias.alias == alias)
    ).first()
    if existing is not None:
        if existing.artifact_cid != cid:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"(source={source!r}, alias={alias!r}) already maps to a "
                    f"different artifact: {existing.artifact_cid.hex()}"
                ),
            )
        return existing
    row = ArtifactAlias(
        artifact_cid=cid,
        source=source,
        alias=alias,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.post(
    "/artifacts/",
    response_model=ArtifactRead,
    status_code=status.HTTP_201_CREATED,
)
def upload(
    session: SessionDep,
    body: bytes = Body(..., media_type="application/octet-stream"),
) -> ArtifactRead:
    artifact = _ingest(session, body)
    return ArtifactRead.from_row(artifact, _aliases_for(session, artifact.cid))


@router.post(
    "/artifacts/upload/",
    response_model=ArtifactRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    session: SessionDep,
    file: UploadFile,
    source: str = Form(default=_DEFAULT_UPLOAD_SOURCE, max_length=MAX_LABEL),
    alias: str | None = Form(default=None, max_length=MAX_LABEL),
) -> ArtifactRead:
    """Convenience: hash an uploaded file and register an alias in one call.

    Reuses the same content-addressing as ``POST /artifacts/`` and the same
    alias upsert/conflict semantics as ``POST /artifacts/{cid_hex}/aliases/``
    (409 on a clashing pair). ``alias`` defaults to the uploaded filename, so a
    bare file upload is captured as ``(source="upload", alias=<filename>)`` and
    is immediately resolvable.
    """
    # Hash in chunks so the upload is never held in memory whole.
    hasher = sha256_hasher()
    while chunk := await file.read(1024 * 1024):
        hasher.update(chunk)
    artifact, _ = record(session, Artifact(sha256=hasher.digest()))
    alias_value = alias or file.filename
    if alias_value:
        _upsert_alias(session, artifact.cid, source, alias_value)
    return ArtifactRead.from_row(artifact, _aliases_for(session, artifact.cid))


# Declared before ``/artifacts/{cid_hex}/`` so the literal "resolve" path is
# not captured by the path parameter.
@router.get("/artifacts/resolve/", response_model=AliasResolveRead)
def resolve(source: str, alias: str, session: SessionDep) -> AliasResolveRead:
    row = session.exec(
        select(ArtifactAlias)
        .where(ArtifactAlias.source == source)
        .where(ArtifactAlias.alias == alias)
    ).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No artifact for source={source!r} alias={alias!r}",
        )
    return AliasResolveRead(cid=row.artifact_cid.hex(), source=source, alias=alias)


def _get_artifact_row(session: Session, cid: bytes, *, hex_value: str) -> Artifact:
    row = session.get(Artifact, cid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {hex_value}")
    return row


@router.get("/artifacts/by-digest/{sha256_hex}/", response_model=ArtifactRead)
def get_by_digest(sha256_hex: str, session: SessionDep) -> ArtifactRead:
    """Look up an artifact by the sha256 of its raw bytes -- the comparison
    path for a client holding a file."""
    digest = cid_from_hex(sha256_hex, field="sha256")
    row = session.exec(select(Artifact).where(Artifact.sha256 == digest)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No artifact with sha256: {sha256_hex}")
    return ArtifactRead.from_row(row, _aliases_for(session, row.cid))


@router.get("/artifacts/{cid_hex}/", response_model=ArtifactRead)
def get(cid_hex: str, session: SessionDep) -> ArtifactRead:
    cid = cid_from_hex(cid_hex)
    row = _get_artifact_row(session, cid, hex_value=cid_hex)
    return ArtifactRead.from_row(row, _aliases_for(session, cid))


@router.patch("/artifacts/{cid_hex}/", response_model=ArtifactRead)
def set_locator(cid_hex: str, body: ArtifactUpdate, session: SessionDep) -> ArtifactRead:
    cid = cid_from_hex(cid_hex)
    row = _get_artifact_row(session, cid, hex_value=cid_hex)
    row.locator = body.locator
    session.add(row)
    session.commit()
    session.refresh(row)
    return ArtifactRead.from_row(row, _aliases_for(session, cid))


@router.post(
    "/artifacts/{cid_hex}/aliases/",
    response_model=AliasRead,
    status_code=status.HTTP_201_CREATED,
)
def add_alias(cid_hex: str, body: AliasCreate, session: SessionDep) -> AliasRead:
    cid = cid_from_hex(cid_hex)
    _get_artifact_row(session, cid, hex_value=cid_hex)
    return AliasRead.from_row(_upsert_alias(session, cid, body.source, body.alias))
