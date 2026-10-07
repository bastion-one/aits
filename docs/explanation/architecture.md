# Architecture

AITS is a reference implementation of the Bastion-One model. The model records
four core ledger facts: an Agent is the stable actor identity; a UAI is the
content-addressed identity of a behavioral configuration; a Data Unique Tag
(DUT) records one inference span or tool call; and a Lineage Tag (LT) records
process progress and links spans into a traceable process.

In this repository, AITS is a small HTTP service that stores those facts,
artifacts, and verification evidence in a content-addressed ledger.
The implementation maps the model terms onto concrete rows: Agent identity is
stored as `Agent`, the UAI is an `AgentConfig` CID, each DUT is stored as
`DataUniqueTag`, and each LT is stored as `LineageTag`.

The implementation has four main layers:

- `app/routers/` exposes the FastAPI surface.
- `app/models.py` defines the SQLModel tables.
- `app/nodes.py` turns typed rows into canonical DAG nodes for hashing and
  verification.
- `app/ledger.py` owns writes, commit-log entries, derived reads, and ledger
  verification helpers.

## Content-addressed nodes

Every ledger object is stored as a node addressed by a CID. The CID is the
SHA-256 hash of the node's canonical content plus its named links. The content
captures the fields that define the object itself; links capture references to
other nodes by CID.

Canonical encoding makes equivalent content hash consistently. AITS uses
RFC 8785 plus a projection for timestamps, UUIDs, bytes, and sets. See the
[ledger format reference](../reference/ledger-format.md) for exact encoding
rules and node shapes.

The core node types are:

| Node | Purpose | Important links |
|------|---------|-----------------|
| `Agent` | Stable agent identity and mutable display name. | none |
| `AgentConfig` | Immutable behavioral configuration; its CID serves as the config's UAI. | none |
| `ConfigActivation` | Append-only claim that an agent should run a config. | `agent`, `config`, optional `prev` |
| `DataUniqueTag` | DUT: one inference span or tool-call record. | `agent`, `config`, `artifacts` |
| `LineageTag` | LT: one lineage DAG watermark node. Graph identity is the root LT CID, derived through `prev`. | `prev`, `derived_from`, optional `dut` |
| `Artifact` | Hash record for bytes read or produced by an agent. | none |

Every node row carries a schema version `v` (1 today), and the node's hashed
content starts with it, so the version is part of the CID. `nodes.to_node()`
picks the assembler by `(table, v)`. Assemblers are append-only code: a new
content shape gets a new `v` and a new assembler, and the old ones stay so old
rows keep verifying. A `v1` assembler reads today's columns for as long as `v1`
rows exist, so the row schema must stay additive: columns are added, never
renamed or removed. A row whose `v` has no assembler fails verification as a
malformed node. `v` is not exposed through the API.

Mutable annotations, such as an agent's display name, are intentionally outside
the CID. They are convenience references, not integrity-bearing ledger facts.

## Write path

Ledger nodes enter through `ledger.stage()`, which assembles the node with
`nodes.to_node()`, computes the CID with `dag.compute_cid()`, inserts the row
if that CID is new, appends a `CommitLogEntry`, and flushes without committing.
`ledger.record()` wraps `stage()` and commits, for callers that own a
single-node write. Inline DUT-and-LT creation stages both rows in one
transaction and commits once. References are validated before staging. If
that transaction fails, neither a new DUT without its LT nor partial
observation entries remain.

This gives the ledger two useful properties:

- Node writes are content-addressed. Submitting the same content and links
  returns the existing node. Identical root content and event time therefore
  dedupe; the API does not mint a unique root per submission. The advisory
  lock serializes the dedup lookup and the commit-head append, so concurrent
  identical submissions dedupe to one node and append one observation each.
- Observations are still counted. Even when a node dedupes, the commit log gets
  a new entry for that submission.

A dedup hit is confirmed, not assumed: `stage()` compares the stored node's
canonical bytes with the submission's, and a mismatch raises `CidCollision`,
which the API returns as 409 with a logged warning. A cross-type collision is
not separately checked, because `type` is inside the hashed content, so it
would also be a full SHA-256 collision.

Content-addressed deduplication is not provider-retry idempotency. Retrying an
ambiguous inference request is not guaranteed to yield the prior inference LT.

The commit log is hash-chained through `(cid, recorded_at, prev_hash, principal)`,
so the ledger can verify both node structure and the order in which the service
observed submissions. `principal` is the identity AITS authenticated for the
request, named by its key in `AUTH_SERVICE_KEYS` (`anonymous` in open mode).
An inference gateway is one such identity; any other holder of a key is another. Authenticated mode records that service identity; open mode accepts
unauthenticated requests and records `anonymous`. The auth dependency records it with
`ledger.attest()` on the request's session, and a write on an unattested
session raises instead of appending an entry nobody vouched for. Rewriting
`principal` breaks the chain.

Concurrent writers serialize commit-head selection in PostgreSQL so the chain
cannot fork. A writer waits at most `COMMIT_LOCK_TIMEOUT_MS` (default 5000) for
that lock; past it, the request fails with 503 and `Retry-After: 1` instead of
waiting indefinitely.

## Lineage graphs

Lineage is represented as a DAG rather than a flat list. `POST /lineage/`
creates a root LT with no `prev`; its CID identifies the graph. Each later
round is `POST /lineage/{root_cid}/nodes/` with an explicit nonempty `prev`.
Cross-graph inputs are exact `derived_from` LT CIDs from another root. The LT
can also link the DUT produced by that round — as an existing `dut_cid`, as an
inline `dut` created in the same transaction, or as neither for a structural
node.

Callers select the root explicitly. Agent identity does not name a current
graph. `GET /agents/{uuid}/sessions/` remains a participation read: it
summarizes graphs that contain a DUT produced by that agent.

Root membership is a fact proved by the graph: follow only `prev` until
reaching an LT without parents. Every path must reach the same root. A
same-root fork can join again; a cross-root join through `prev` is rejected.
`derived_from` records consumed work from another graph without changing
membership. Traceback follows both kinds of edges because provenance can
cross graph boundaries.

The root is derived on reads rather than stored in each LT's content or row.
This avoids duplicating a fact already established by immutable links. There
is currently no persisted root index; any future index would be derived,
rebuildable data outside the content-addressed node.

Explicit root and parent selection lets concurrent work by the same agent
proceed without competing for a mutable current-graph pointer. AITS validates
the links; the caller owns workflow state and parent selection. Inference
headers, gateway processing, and telemetry belong to downstream integrations.

See the [lineage reference](../reference/lineage.md) for request and response
contracts, defaults, graph reads, and error statuses.

## Event time and observation time

`occurred_at` is the caller's asserted event time and is part of node content.
`recorded_at` is the service's observation time in the commit log. A repeated
submission can therefore keep the same node CID while adding another dated
observation. Event-time ordering and service observation order answer different
questions; neither replaces the other.

The service rejects an `occurred_at` more than `OCCURRED_AT_MAX_SKEW_SECONDS`
(default 300) ahead of its clock, because a forward-dated event would reorder
traceback and still verify. It accepts past times: a recorder that replays
saved records after an outage submits them late, and the gap between
`occurred_at` and `recorded_at` shows how late.

## Verification

`GET /verify/{cid}/` recomputes the addressed node, recursively verifies every
linked node, and checks temporal order against the commit log. `GET /audit/`
walks every stored node, checks temporal order, reports observed CIDs with no
stored node, and verifies the commit-log chain. The temporal check fails closed:
a node, or a link target, with no commit-log observation is a finding rather
than a pass. A stored node without an observation is reported per node as
`unlogged_node`, by both `/verify/{cid}/` and `/audit/`; `missing_node` means an
observed CID has no stored node. Repeated observations of a deduplicated node
remain valid.

A verifier outside AITS can keep its own anchors of the commit log.
`GET /ledger/head` returns the latest entry's `seq` and `entry_hash`, and the
first entry's `entry_hash` as `ledger_id`, all null on an empty log; AITS
derives them per request and stores nothing. `GET /commits/?after_seq=N`
returns entries with `seq > N` in ascending order, at most 1000 per request,
so the verifier pages from its last anchor to the head and checks that each
entry's `prev_hash` is its predecessor's `entry_hash`.

Every full-audit read uses one fresh session and one committed database snapshot,
including recursive link resolution, temporal checks, and response counts.
PostgreSQL uses a read-only repeatable-read transaction. SQLite explicitly begins
a read transaction. The first data read establishes the snapshot, and later
commits are visible to a subsequent audit. The audit releases its transaction on
completion or failure. Ordinary sessions retain their existing behavior.

With SQLite's default rollback journal, this read transaction can delay writer
commits or cause lock timeouts. WAL permits concurrent reader and writer
transactions; the audit does not change the database's journal configuration.
Keep audit transactions limited to verification work. Long-lived PostgreSQL
snapshots retain older row versions until the audit ends.

Audit validity describes internal consistency of the examined state. Matching
nodes and observations do not independently establish authorized admission,
preserved history, or complete capture of AI executions.

If a row is edited after recording, its recomputed CID no longer matches the
address where it is stored. If a privileged actor changes the row and moves it
to a newly computed CID, downstream links still point to the original CID, so
the tamper becomes a dangling link unless the attacker rewrites the entire
reachable graph and commit history.

## Ledger format record

The `ledger_format` table holds one row naming the ledger format
(`{"format": 1, "hash": "sha2-256", "canonical": "rfc8785"}`). Startup writes it
when the row is missing and there are no commit-log entries. It refuses
to start if entries exist without a format record, or if the record differs
from the supported format. The record is outside the node hashes; startup
checks it to avoid writing to a ledger with an incompatible format.

## API and generated client

FastAPI generates `openapi.json` from the in-process app. The Python SDK is
generated from that schema by `make regen`, using the generator inputs in
`clients/build/`. The generated tree under `clients/python/` is ephemeral and is
not committed.

Use `make build-client` to build the generated SDK distribution. A top-level
`uv build` is not the client build path.

## Schema lifecycle

At startup, the service calls `SQLModel.metadata.create_all()` to create missing
tables. That keeps the reference implementation easy to run locally and in
tests. It is not intended to be the long-term schema migration story for a
production deployment.

No migration tooling is supplied. Version 0.3.0 requires a fresh database;
retain the prior deployment and database for historical verification. See
[the upgrade guide](../howto/migrate-to-root-lt-lineage.md).
