"""Endpoints for ``LineageTag`` graph watermark nodes.

A lineage graph is a DAG identified by its root LT CID. Every non-root node
names at least one ``prev`` parent in the same graph (fork on parallel work,
join on synthesis). ``derived_from`` carries exact cross-graph LT CIDs.
``dut`` links the span the round produced, or is absent for a structural
node. Membership and the frontier are derived from ``prev`` only.

``POST /lineage/`` creates a root. ``POST /lineage/{root_cid}/nodes/`` is the
unified write for structural nodes, existing-DUT attachment, and atomic
inline DUT creation. Verification is the generic ``GET /verify/{cid}/``.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from .. import ledger
from ..db import SessionDep
from ..models import DataUniqueTag, LineageTag
from ..nodes import hex_cid_list
from .common import MAX_ITEMS, Label, LongText, cid_from_hex, ensure_aware, get_agent_row
from .duts import DUTCreate, DUTRead, build_dut_row

router = APIRouter(tags=["lineage"])

_NODE_ERRORS = {
    400: {"description": "Malformed CID, non-root path, or invalid root relationship"},
    404: {"description": "Referenced LT or DUT is absent or the wrong kind"},
    409: {"description": "Inline DUT has no active agent configuration"},
}


class LineageRootCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: Label
    step_id: Label
    transformation: Label
    occurred_at: datetime | None = None


class InlineDUTCreate(BaseModel):
    """DUT business fields for atomic inline creation with a lineage node.

    ``config_cid`` defaults to the agent's activation head. ``occurred_at``
    defaults to the enclosing LT event time. ``sequence`` is 0; business-key
    and artifact lists default empty. ``config_cid`` and each artifact CID
    must be exactly 64 hexadecimal characters of either case.
    """

    model_config = ConfigDict(extra="forbid")

    agent_uuid: UUID
    span_id: Label
    input_context: LongText
    agent_output: LongText
    sequence: int = 0
    business_object_keys: list[Label] = Field(default_factory=list, max_length=MAX_ITEMS)
    artifact_cids: list[str] = Field(
        default_factory=list,
        max_length=MAX_ITEMS,
        description=(
            "hex CIDs of artifacts; each must be exactly 64 hexadecimal "
            "characters of either case"
        ),
    )
    config_cid: str | None = Field(
        default=None,
        description=(
            "hex CID of the behavioral AgentConfig; must be exactly 64 "
            "hexadecimal characters of either case when supplied"
        ),
    )
    occurred_at: datetime | None = None


class LineageNodeCreate(BaseModel):
    """Create a non-root lineage node under an existing root.

    ``dut_cid`` and ``dut`` are mutually exclusive. Omit both for a
    structural node; supply ``dut_cid`` to attach an existing DUT; supply
    ``dut`` to create a DUT and this node in one transaction. Enforcement of
    the mutual-exclusion rule is in the handler and returns 400.
    """

    model_config = ConfigDict(extra="forbid")

    actor_id: Label
    step_id: Label
    transformation: Label
    occurred_at: datetime | None = None
    prev: list[str] = Field(
        min_length=1,
        max_length=MAX_ITEMS,
        description="hex CIDs of this node's intra-graph causal predecessors",
    )
    derived_from: list[str] = Field(
        default_factory=list,
        max_length=MAX_ITEMS,
        description="exact hex CIDs of cross-graph lineage nodes this round consumed",
    )
    dut_cid: str | None = Field(
        default=None,
        description=(
            "hex CID of an existing DUT to attach. Mutually exclusive with dut; "
            "omit both for a structural node."
        ),
    )
    dut: InlineDUTCreate | None = Field(
        default=None,
        description=(
            "inline DUT to create atomically with this node. Mutually exclusive "
            "with dut_cid; omit both for a structural node."
        ),
    )


class LineageRead(BaseModel):
    cid: str
    root: str = Field(description="derived root LT CID for this node's prev ancestry")
    actor_id: str
    step_id: str
    transformation: str
    occurred_at: datetime
    prev: list[str]
    derived_from: list[str]
    dut: str | None = Field(
        default=None,
        description="CID of the attached DUT, or null for a structural node",
    )

    @classmethod
    def from_row(cls, row: LineageTag, root: str) -> "LineageRead":
        return cls(
            cid=row.cid.hex(),
            root=root,
            actor_id=row.actor_id,
            step_id=row.step_id,
            transformation=row.transformation,
            occurred_at=row.occurred_at,
            prev=row.prev_cids,
            derived_from=row.derived_from_cids,
            dut=row.dut_cid.hex() if row.dut_cid is not None else None,
        )


class LineageNodeRead(BaseModel):
    root: str = Field(description="path root LT CID; equals node.root after commit")
    node: LineageRead
    dut: DUTRead | None = Field(
        default=None,
        description=(
            "full DUT body when this node attaches a DUT; null for a structural "
            "node. Distinct from node.dut, which is a CID or null."
        ),
    )


class LineageGraphRead(BaseModel):
    root: str = Field(description="CID of the root LT that identifies this graph")
    nodes: list[LineageRead]
    frontier: list[str]


class LineageGraphSummary(BaseModel):
    root: str
    nodes: int
    started_at: datetime
    last_occurred_at: datetime
    frontier: list[str]


class WatermarkRead(BaseModel):
    cid: str
    watermark: str


class TracebackStep(BaseModel):
    occurred_at: datetime
    dut_cid: str
    agent_output: str
    agent_cid: str
    config_cid: str


def _get_lt_row(session: Session, cid: bytes, *, field: str, hex_value: str) -> LineageTag:
    row = session.get(LineageTag, cid)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Lineage node not found in {field}: {hex_value}"
        )
    return row


def _raise_graph_error(exc: ledger.LineageGraphError, *, field: str) -> None:
    hex_value = exc.cid.hex()
    if isinstance(exc, ledger.MissingLineageTag):
        raise HTTPException(
            status_code=404, detail=f"Lineage node not found in {field}: {hex_value}"
        ) from exc
    if isinstance(exc, ledger.ConflictingRoots):
        roots = ", ".join(sorted(r.hex() for r in exc.roots))
        raise HTTPException(
            status_code=400,
            detail=f"{field} {hex_value} resolves to multiple roots: {roots}",
        ) from exc
    if isinstance(exc, ledger.CyclicLineage):
        raise HTTPException(
            status_code=400,
            detail=f"{field} {hex_value} has cyclic prev ancestry",
        ) from exc
    raise exc


def _resolve_root(
    session: Session,
    cid: bytes,
    *,
    field: str,
    memo: dict[bytes, bytes] | None = None,
) -> bytes:
    try:
        return ledger.resolve_root(session, cid, memo=memo)
    except ledger.LineageGraphError as exc:
        _raise_graph_error(exc, field=field)
        raise  # pragma: no cover


def graph_summary(root_cid: bytes, rows: list[LineageTag]) -> LineageGraphSummary:
    return LineageGraphSummary(
        root=root_cid.hex(),
        nodes=len(rows),
        started_at=min(r.occurred_at for r in rows),
        last_occurred_at=max(r.occurred_at for r in rows),
        frontier=hex_cid_list(ledger.graph_frontier(rows)),
    )


def sort_summaries(summaries: list[LineageGraphSummary]) -> list[LineageGraphSummary]:
    summaries.sort(key=lambda item: (item.last_occurred_at, item.root), reverse=True)
    return summaries


def _ordered_nodes(root: str, rows: list[LineageTag]) -> list[LineageRead]:
    ordered = sorted(rows, key=lambda row: (row.occurred_at, row.cid.hex()))
    return [LineageRead.from_row(row, root=root) for row in ordered]


def _inline_dut_create(
    session: Session, inline: InlineDUTCreate, occurred_at: datetime
) -> DUTCreate:
    get_agent_row(session, inline.agent_uuid)
    if inline.config_cid is None:
        head = ledger.activation_head(session, inline.agent_uuid)
        if head is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Agent {inline.agent_uuid} has no active config; "
                    "pass config_cid or activate one first"
                ),
            )
        config_cid = head.config_cid.hex()
    else:
        parsed = cid_from_hex(inline.config_cid, field="dut.config_cid")
        config_cid = parsed.hex()
    artifact_cids = []
    for value in inline.artifact_cids:
        parsed = cid_from_hex(value, field="dut.artifact_cids")
        artifact_cids.append(parsed.hex())
    return DUTCreate(
        agent_uuid=inline.agent_uuid,
        config_cid=config_cid,
        span_id=inline.span_id,
        business_object_keys=inline.business_object_keys,
        sequence=inline.sequence,
        input_context=inline.input_context,
        agent_output=inline.agent_output,
        occurred_at=occurred_at,
        artifact_cids=artifact_cids,
    )


def _require_root(session: Session, root_cid: str) -> bytes:
    cid = cid_from_hex(root_cid, field="root_cid")
    row = _get_lt_row(session, cid, field="root_cid", hex_value=root_cid.lower())
    if row.prev_cids:
        raise HTTPException(status_code=400, detail=f"root_cid is not a root LT: {cid.hex()}")
    return cid


@router.get("/lineage/", response_model=list[LineageGraphSummary])
def list_roots(session: SessionDep, limit: int = 100) -> list[LineageGraphSummary]:
    """Enumerate lineage graphs by derived root, most recently active first."""
    try:
        grouped = ledger.graphs_by_root(session)
    except ledger.LineageGraphError as exc:
        _raise_graph_error(exc, field="cid")
    summaries = [graph_summary(root, rows) for root, rows in grouped.items()]
    return sort_summaries(summaries)[:limit]


@router.post("/lineage/", response_model=LineageRead, status_code=status.HTTP_201_CREATED)
def create_root(body: LineageRootCreate, session: SessionDep) -> LineageRead:
    """Create a root LT. Identical content and event time dedupe to one CID."""
    row = LineageTag(
        actor_id=body.actor_id,
        step_id=body.step_id,
        transformation=body.transformation,
        occurred_at=ensure_aware(body.occurred_at, field="occurred_at"),
    )
    stored, _ = ledger.record(session, row)
    root = stored.cid.hex()
    return LineageRead.from_row(stored, root=root)


@router.get("/lineage/nodes/{lt_cid}/", response_model=LineageRead)
def get_lineage_node(lt_cid: str, session: SessionDep) -> LineageRead:
    cid = cid_from_hex(lt_cid, field="lt_cid")
    row = _get_lt_row(session, cid, field="lt_cid", hex_value=cid.hex())
    root = _resolve_root(session, cid, field="lt_cid")
    return LineageRead.from_row(row, root=root.hex())


@router.get("/lineage/nodes/{lt_cid}/watermark/", response_model=WatermarkRead)
def watermark(lt_cid: str, session: SessionDep) -> WatermarkRead:
    """Render the flat P1 watermark string straight off a node: its
    ``derived_from`` parents (each head's produced DUT when it has one), the
    transformation, and the asserted event time."""
    cid = cid_from_hex(lt_cid, field="lt_cid")
    row = _get_lt_row(session, cid, field="lt_cid", hex_value=cid.hex())
    parents = []
    for value in row.derived_from_cids:
        target = bytes.fromhex(value)
        lt = session.get(LineageTag, target)
        if lt is not None and lt.dut_cid is not None:
            target = lt.dut_cid
        parents.append(target.hex()[:8])
    ts = row.occurred_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    rendered = f"Parent:[{', '.join(sorted(parents))}]_Transformation:[{row.transformation}]_{ts}"
    return WatermarkRead(cid=cid.hex(), watermark=rendered)


@router.get("/lineage/nodes/{lt_cid}/traceback/", response_model=list[TracebackStep])
def trace_back(lt_cid: str, session: SessionDep) -> list[TracebackStep]:
    """Every contributing DUT in the node's ancestry, ordered by event time,
    each carrying its agent + config attribution. Follows ``prev`` and
    ``derived_from``; DUTs are collected from LT attachments."""
    cid = cid_from_hex(lt_cid, field="lt_cid")
    _get_lt_row(session, cid, field="lt_cid", hex_value=cid.hex())
    return [
        TracebackStep(
            occurred_at=dut.occurred_at,
            dut_cid=dut.cid.hex(),
            agent_output=dut.agent_output,
            agent_cid=dut.agent_cid.hex(),
            config_cid=dut.config_cid.hex(),
        )
        for dut in ledger.traceback(session, cid)
    ]


@router.post(
    "/lineage/{root_cid}/nodes/",
    response_model=LineageNodeRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {
            "description": "Lineage node created",
            "headers": {
                "Location": {
                    "description": "Canonical URL of the created lineage node",
                    "schema": {"type": "string"},
                }
            },
        },
        **_NODE_ERRORS,
    },
)
def create_lineage_node(
    root_cid: str, body: LineageNodeCreate, session: SessionDep, response: Response
) -> LineageNodeRead:
    """Create a non-root LT under ``root_cid``.

    Validates every link before storage. When ``dut`` is supplied, the DUT,
    LT, and both observation entries commit together.
    """
    if body.dut_cid is not None and body.dut is not None:
        raise HTTPException(status_code=400, detail="dut_cid and dut are mutually exclusive")

    root = _require_root(session, root_cid)
    memo: dict[bytes, bytes] = {root: root}
    occurred_at = ensure_aware(body.occurred_at, field="occurred_at")

    prev: set[bytes] = set()
    for value in body.prev:
        cid = cid_from_hex(value, field="prev")
        _get_lt_row(session, cid, field="prev", hex_value=cid.hex())
        parent_root = _resolve_root(session, cid, field="prev", memo=memo)
        if parent_root != root:
            raise HTTPException(
                status_code=400,
                detail=f"prev {cid.hex()} has root {parent_root.hex()}, not {root.hex()}",
            )
        prev.add(cid)

    derived: set[bytes] = set()
    for value in body.derived_from:
        cid = cid_from_hex(value, field="derived_from")
        _get_lt_row(session, cid, field="derived_from", hex_value=cid.hex())
        source_root = _resolve_root(session, cid, field="derived_from", memo=memo)
        if source_root == root:
            raise HTTPException(
                status_code=400,
                detail=f"derived_from {cid.hex()} has the target root {root.hex()}",
            )
        derived.add(cid)

    existing_dut: DataUniqueTag | None = None
    inline_row: DataUniqueTag | None = None
    dut_cid: bytes | None = None
    if body.dut_cid is not None:
        dut_cid = cid_from_hex(body.dut_cid, field="dut_cid")
        existing_dut = session.get(DataUniqueTag, dut_cid)
        if existing_dut is None:
            raise HTTPException(
                status_code=404, detail=f"DUT not found in dut_cid: {dut_cid.hex()}"
            )
    elif body.dut is not None:
        dut_occurred = (
            occurred_at
            if body.dut.occurred_at is None
            else ensure_aware(body.dut.occurred_at, field="dut.occurred_at")
        )
        inline_row = build_dut_row(session, _inline_dut_create(session, body.dut, dut_occurred))

    committed = False
    stored: LineageTag | None = None
    attached: DataUniqueTag | None = existing_dut
    try:
        if inline_row is not None:
            attached, _ = ledger.stage(session, inline_row)
            dut_cid = attached.cid
        stored, _ = ledger.stage(
            session,
            LineageTag(
                actor_id=body.actor_id,
                step_id=body.step_id,
                transformation=body.transformation,
                occurred_at=occurred_at,
                dut_cid=dut_cid,
                prev_cids=hex_cid_list(prev),
                derived_from_cids=hex_cid_list(derived),
            ),
        )
        session.commit()
        committed = True
        session.refresh(stored)
        if attached is not None:
            session.refresh(attached)
    finally:
        if not committed:
            session.rollback()

    assert stored is not None
    root_hex = root.hex()
    response.headers["Location"] = f"/lineage/nodes/{stored.cid.hex()}/"
    return LineageNodeRead(
        root=root_hex,
        node=LineageRead.from_row(stored, root=root_hex),
        dut=DUTRead.from_row(attached) if attached is not None else None,
    )


@router.get("/lineage/{root_cid}/", response_model=LineageGraphRead)
def get_lineage_graph(root_cid: str, session: SessionDep) -> LineageGraphRead:
    root = _require_root(session, root_cid)
    try:
        grouped = ledger.graphs_by_root(session)
    except ledger.LineageGraphError as exc:
        _raise_graph_error(exc, field="root_cid")
    rows = grouped.get(root)
    if not rows:
        raise HTTPException(
            status_code=404, detail=f"Lineage node not found in root_cid: {root.hex()}"
        )
    root_hex = root.hex()
    return LineageGraphRead(
        root=root_hex,
        nodes=_ordered_nodes(root_hex, rows),
        frontier=hex_cid_list(ledger.graph_frontier(rows)),
    )
