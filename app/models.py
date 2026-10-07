"""SQLModel tables: typed rows for content-addressed DAG nodes, plus the
derived reverse index and the commit log.

Every node table's primary key is the node's **CID** (raw 32-byte SHA-256,
computed by ``app.dag.compute_cid`` over the node assembly in ``app.nodes``).
Every node row also carries ``v``, the version of its content shape; it is
hashed with the content and selects the assembler (see ``app.nodes``).
Link targets are stored as typed columns on the owning row -- single-valued
roles as raw-bytes columns, multi-valued roles as JSON lists of hex CIDs --
so the rows alone are sufficient to rebuild every derived structure.
``CommitLogEntry`` is the append-only, hash-chained record of when each
submission was observed.

Two clocks: ``occurred_at`` (self-asserted event time, inside the CID) lives
on event rows; ``recorded_at`` (server observation) lives only in the commit
log. Datetime columns use :class:`app.types.UTCDateTime`, which enforces the
aware-UTC contract and round-trips microseconds on both sqlite and postgres.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    JSON,
    Integer,
    LargeBinary,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel

from .types import UTCDateTime


def _cid_pk() -> Column:
    return Column(LargeBinary, primary_key=True)


def _schema_version() -> Column:
    return Column(Integer, nullable=False, server_default="1")


def _cid_col(*, nullable: bool = False, index: bool = False) -> Column:
    return Column(LargeBinary, nullable=nullable, index=index)


class Agent(SQLModel, table=True):
    """Agent genesis node: ``{type, uuid, created_at}`` -> CID.

    ``uuid`` is the stable ref-namespace key clients address agents by.
    ``name`` is a mutable ref-layer annotation -- outside the hash,
    overwritten in place, no history. Callers select a lineage root
    explicitly; agent identity does not name a current graph.
    """

    __tablename__ = "agent"
    __table_args__ = (UniqueConstraint("uuid", name="uq_agent_uuid"),)

    cid: bytes = Field(default=b"", sa_column=_cid_pk())
    v: int = Field(default=1, sa_column=_schema_version())
    uuid: UUID = Field(index=True)
    created_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))
    name: str


class AgentConfig(SQLModel, table=True):
    """Behavioral-only config value object; the CID is the UAI.

    Content carries behavioral fields only -- no agent reference, no time --
    so identical configurations dedupe globally across agents.
    """

    __tablename__ = "agent_config"

    cid: bytes = Field(default=b"", sa_column=_cid_pk())
    v: int = Field(default=1, sa_column=_schema_version())
    system_prompt: str
    llm_config: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    tools: list[Any] = Field(default_factory=list, sa_column=Column(JSON))
    config_metadata: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column("config_metadata", JSON)
    )


class ConfigActivation(SQLModel, table=True):
    """One link in an agent's append-only activation chain (the *should*).

    Links: ``{agent, config, prev?}``. The head is derived (the activation no
    other activation names as ``prev``); appends are compare-and-set on the
    head, and the unique constraint on ``prev_cid`` makes forks impossible at
    the storage layer.
    """

    __tablename__ = "config_activation"
    __table_args__ = (UniqueConstraint("prev_cid", name="uq_config_activation_prev"),)

    cid: bytes = Field(default=b"", sa_column=_cid_pk())
    v: int = Field(default=1, sa_column=_schema_version())
    agent_uuid: UUID = Field(index=True)
    agent_cid: bytes = Field(sa_column=_cid_col())
    config_cid: bytes = Field(sa_column=_cid_col(index=True))
    prev_cid: bytes | None = Field(default=None, sa_column=_cid_col(nullable=True))


class DataUniqueTag(SQLModel, table=True):
    """DUT span node: one inference round / tool call, as an immutable event.

    Content: ``{type, span_id, business_object_keys, sequence, input_context,
    agent_output, occurred_at}`` -- the text roundtrip's integrity *is* the
    CID. Links: ``{agent, config, artifacts?}``; the artifact CID set folds
    into the hash, so de-linking an artifact changes the DUT's identity.
    """

    __tablename__ = "dut"

    cid: bytes = Field(default=b"", sa_column=_cid_pk())
    v: int = Field(default=1, sa_column=_schema_version())
    span_id: str
    business_object_keys: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    sequence: int
    input_context: str
    agent_output: str
    occurred_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))
    agent_cid: bytes = Field(sa_column=_cid_col(index=True))
    config_cid: bytes = Field(sa_column=_cid_col(index=True))
    artifact_cids: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class LineageTag(SQLModel, table=True):
    """Lineage-DAG watermark node: one append per round.

    Content: ``{type, actor_id, step_id, transformation, occurred_at}``.
    Links: ``{prev?, derived_from?, dut?}`` -- ``prev`` is the multi-valued
    intra-graph causal edge set (fork/join), ``derived_from`` the multi-valued
    cross-graph merge edge set, ``dut`` the span this round produced. A root
    LT has no ``prev``; its CID identifies the graph. Membership and the
    frontier are derived from ``prev``, never stored.
    """

    __tablename__ = "lineage_tag"

    cid: bytes = Field(default=b"", sa_column=_cid_pk())
    v: int = Field(default=1, sa_column=_schema_version())
    actor_id: str
    step_id: str
    transformation: str
    occurred_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))
    dut_cid: bytes | None = Field(default=None, sa_column=_cid_col(nullable=True))
    prev_cids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    derived_from_cids: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class Artifact(SQLModel, table=True):
    """Content-addressed byte object, bytes dropped after hashing.

    Content: ``{type, sha256}`` where ``sha256`` is the digest of the raw
    object bytes -- a timeless value object, so identical bytes dedupe. The
    two hashes serve different lookups: ``sha256`` answers "have we seen
    these bytes" (clients re-hash a file and query by digest), while ``cid``
    is the node identity DUTs link in the DAG. ``locator`` is a mutable
    ref-layer pointer to wherever the data lives; it is outside the CID, so
    data can move without changing identity.
    """

    __tablename__ = "artifact"

    cid: bytes = Field(default=b"", sa_column=_cid_pk())
    v: int = Field(default=1, sa_column=_schema_version())
    sha256: bytes = Field(sa_column=Column(LargeBinary, nullable=False, unique=True))
    locator: str | None = Field(default=None)


class ArtifactAlias(SQLModel, table=True):
    """A provider-issued ``(source, alias)`` name for a known :class:`Artifact`.

    Captures the upload-once / reference-many file ids inference providers
    return (e.g. an ``anthropic`` ``file_...`` id), and plain file names. The
    ``(source, alias)`` pair is globally unique.
    """

    __tablename__ = "artifact_alias"
    __table_args__ = (UniqueConstraint("source", "alias", name="uq_artifact_alias_source_alias"),)

    id: int | None = Field(default=None, primary_key=True)
    artifact_cid: bytes = Field(
        sa_column=Column(
            LargeBinary,
            ForeignKey("artifact.cid"),
            index=True,
            nullable=False,
        ),
    )
    source: str
    alias: str
    created_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))


class CommitLogEntry(SQLModel, table=True):
    """Append-only, hash-chained observation log: one entry per submission.

    ``recorded_at`` is server-assigned and lives only here -- never inside a
    CID. Node submissions dedupe on content; the log still appends, so every
    observation stays countable. ``entry_hash`` chains over
    ``(cid, recorded_at, prev_hash, principal)``; the head is
    externally anchorable. Like ``recorded_at``, attribution lives here rather
    than in a node CID. ``principal`` is the identity AITS authenticated for the
    request, named by its key; AITS records nothing it did not authenticate.

    ``ledger.stage`` serializes appends under an advisory lock. The unique
    constraint on ``prev_hash`` is the storage-level backstop: two entries
    that chain to the same predecessor cannot both commit, so the log cannot
    fork even if a write path skips the lock.
    """

    __tablename__ = "commit_log"
    __table_args__ = (UniqueConstraint("prev_hash", name="uq_commit_log_prev_hash"),)

    seq: int | None = Field(default=None, primary_key=True)
    cid: bytes = Field(sa_column=_cid_col(index=True))
    recorded_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))
    prev_hash: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    entry_hash: bytes = Field(sa_column=Column(LargeBinary, nullable=False, unique=True))
    principal: str
    """The identity AITS authenticated for the request that created this entry: the
    name of the key from ``AUTH_SERVICE_KEYS`` (``anonymous`` in open mode)."""


class LedgerFormat(SQLModel, table=True):
    """The ledger format record: one row, written at first startup, checked at every startup.

    ``ledger.init_ledger`` writes ``ledger.FORMAT`` here into a fresh database and
    refuses to start if the row is missing from a non-empty ledger or differs.
    Nothing in it is hashed, because a wrong format already makes every CID fail
    to verify.
    """

    __tablename__ = "ledger_format"
    __table_args__ = (CheckConstraint("id = 1", name="ck_ledger_format_one_row"),)

    id: int = Field(primary_key=True)
    format: int = Field(nullable=False)
    hash: str = Field(nullable=False)
    canonical: str = Field(nullable=False)
