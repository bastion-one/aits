# Architecture

AITS is a reference implementation of the Bastion-One model. The model records
four core ledger facts: an Agent is the stable actor identity; a UAI is the
content-addressed identity of a behavioral configuration; a Data Unique Tag
(DUT) records one inference span or tool call; and a Lineage Tag (LT) records
session progress and links spans into a traceable process.

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

The core node types are:

| Node | Purpose | Important links |
|------|---------|-----------------|
| `Agent` | Stable agent identity and mutable display name. | none |
| `AgentConfig` | Immutable behavioral configuration; its CID serves as the config's UAI. | none |
| `ConfigActivation` | Append-only claim that an agent should run a config. | `agent`, `config`, optional `prev` |
| `DataUniqueTag` | DUT: one inference span or tool-call record. | `agent`, `config`, `artifacts` |
| `LineageTag` | LT: one session DAG watermark node. | `prev`, `derived_from`, optional `dut` |
| `Artifact` | Hash record for bytes read or produced by an agent. | none |

Mutable annotations, such as an agent's display name or current session pointer,
are intentionally outside the CID. They are convenience references, not
integrity-bearing ledger facts.

## Write path

All ledger nodes enter through `ledger.record()`. The write path assembles the
node with `nodes.to_node()`, computes the CID with `dag.compute_cid()`, inserts
the row if that CID is new, and always appends a `CommitLogEntry`.

This gives the ledger two useful properties:

- Node writes are idempotent. Submitting the same content and links returns the
  existing node.
- Observations are still counted. Even when a node dedupes, the commit log gets
  a new entry for that submission.

The commit log is hash-chained through `(cid, recorded_at, prev_hash)`, so the
ledger can verify both node structure and the order in which the service
observed submissions.

## Lineage sessions

Lineage is represented as a DAG rather than a flat list. A session starts with a
genesis LT (`LineageTag`), then each round appends another LT whose `prev` links
point to the current session frontier. Cross-session inputs are recorded through
`derived_from`, which lets one agent consume another agent's session without
copying its history. The LT can also link the DUT produced by that round, so the
session watermark and the immutable span record remain separate but connected.

The agent-scoped endpoint `POST /agents/{uuid}/record/` is the convenience path:
it records a DUT and an LT in one call, using the agent's active config CID
(the `AgentConfig` UAI) and current session. The session-scoped endpoint
`POST /lineage/{session_uuid}/record/` exposes the same operation when callers
need explicit session control.

## Verification

`GET /verify/{cid}/` recomputes the addressed node, recursively verifies every
linked node, and checks temporal order against the commit log. `GET /audit/`
walks every stored node and verifies the commit-log chain.

If a row is edited after recording, its recomputed CID no longer matches the
address where it is stored. If a privileged actor changes the row and moves it
to a newly computed CID, downstream links still point to the original CID, so
the tamper becomes a dangling link unless the attacker rewrites the entire
reachable graph and commit history.

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

Alembic migrations are planned. Until they are added, treat schema evolution as
part of the reference implementation's development lifecycle and reset or
migrate development databases deliberately when model definitions change.
