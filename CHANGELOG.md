# Changelog

## Unreleased

### Added

- `GET /ledger/head` returns `{"ledger_id", "seq", "entry_hash"}`: the `seq`
  and `entry_hash` of the latest commit-log entry, and the first entry's
  `entry_hash` as `ledger_id`. All three are null on an empty log. It is
  derived per request and requires a service key.
- `GET /commits/` takes `after_seq` and returns only entries with a greater
  `seq`, so an external verifier can page through the log. It combines with
  the `cid` filter.

### Changed

- `GET /commits/` caps `limit` at 1000 (default still 100); a larger or
  non-positive `limit` is a 422. A caller that relied on a larger limit breaks
  and must page with `after_seq`.

## 0.3.0

Breaking pre-1.0 release: format 1. The service and generated `aits-client`
metadata use 0.3.0. This release requires a fresh database.
There is no migration. Content and commit-log entry hashes are computed over
the RFC 8785 canonical encoding, every commit-log entry hash covers the
attested `principal`, `commit_log.prev_hash` carries a unique constraint, a
dedup hit is confirmed by byte comparison (a mismatch returns 409), a one-row
`ledger_format` table records the format, and every node's hashed content
carries a `v` schema version; each changes stored bytes, the schema, or write
behavior. Every resource endpoint also requires a service key, and the
server refuses to start without one unless `AUTH_DISABLED=true`.

### Added

- Maintained reference pages for runtime configuration, record operations,
  verification findings, and ledger-format encoding; an inline-DUT how-to;
  and an isolated tutorial setup with per-step checkpoints. Upgrade guidance
  now covers the current 0.3.0 service and client.
- `docs/reference/ledger-format.md` lists the hashed content and link roles of
  every node type and the timestamp form, so a verifier outside AITS can
  recompute any CID from API reads.
- GitHub Actions CI (`.github/workflows/ci.yml`) runs the format check, pylint,
  the test suite against PostgreSQL 16, and an `openapi.json` drift check on
  pull requests and pushes to `main`.
- `GET /ready` checks that the database is reachable and returns 503 if it is
  not. `GET /health` stays a liveness check that does not touch the database.
- `GET /audit/` reports `nodes_verified`: the number of nodes that passed both the
  structural (hash) check and the temporal-order check. It is lower than
  `nodes_checked` when any node fails, and a node whose linked node failed is
  not counted.
- The Claude ledger hook sends `AITS_API_KEY` as a Bearer token, and its
  `status` command reports whether a key is set. `tools/aits_view.py` reads
  the same variable or takes `--api-key`.
- The ledger format record: a one-row `ledger_format` table (`format`,
  `hash`, `canonical`) holding `{"format": 1, "hash": "sha2-256",
  "canonical": "rfc8785"}`. At startup `init_db` writes it into a fresh
  database and otherwise refuses to start unless the row matches, so a
  pre-format-1 database (commit-log entries, no record) or a different-format
  database fails with a clear error. Nothing in it is hashed.
  - Breaking: a fresh database is required; there is no migration. This ships
    in the format-1 release.
- Every commit-log entry records `principal`, the identity AITS authenticated
  for the request, named by its key in `AUTH_SERVICE_KEYS` (`anonymous` in
  open mode). An inference gateway is one such identity; any other holder
  of a key is another. AITS records nothing it did not authenticate. `principal` is part
  of the entry hash and returned by `GET /commits/`. Writes on a session
  without attribution raise; scripts call `ledger.attest()` first.
  - Breaking: `commit_log.principal` is NOT NULL and the entry hash now covers
    it, so a fresh database is required. There is no migration; an existing
    database fails with a missing-column error. This ships in the format-1
    release with RFC 8785 canonicalization, the unique `prev_hash`
    constraint, the collision check, the format record and the schema
    version.
  - The auth dependency marks the request's session with the principal; this
    is a dictionary write, not a database round trip.
- Service authentication. Callers send `Authorization: Bearer <secret>`, with
  a secret from `AUTH_SERVICE_KEYS` (comma-separated `name:secret` pairs).
  Missing or wrong keys return 401. `/health`, `/docs`, and `/openapi.json`
  stay open. The OpenAPI schema declares a bearer security scheme, so the
  generated SDK accepts `Configuration(access_token=...)`.
- `AUTH_DISABLED=true` opts into unauthenticated open mode for local
  development. The `.env.example` template sets it.

### Changed

- `GET /agents/` returns agents in registration order, one page at a time:
  `limit` (default 100, at most 1000) and `offset`. Callers that relied on
  receiving the full list are now truncated to the first 100 agents and must
  page with `offset`. `tools/aits_view.py` pages through all agents.
- `POST /artifacts/upload/` hashes the file in 1 MiB chunks instead of reading
  it into memory whole.
- `.gitignore` ignores local `*.db` files.
- A writer waits at most `COMMIT_LOCK_TIMEOUT_MS` (default 5000) for the
  PostgreSQL commit lock. Past that, the request returns 503 with
  `Retry-After: 1` instead of waiting indefinitely.
- `occurred_at` more than `OCCURRED_AT_MAX_SKEW_SECONDS` (default 300) ahead
  of the server clock returns 400. A forward-dated event time was sealed into
  the CID and reordered traceback while still verifying. Past times are still
  accepted, so saved records can be replayed.
- Request bodies larger than `MAX_REQUEST_BYTES` (default 64 MiB) return 413.
  The cap applies to declared and chunked bodies alike, and bounds artifact
  uploads.
- Request fields have length limits, returning 422 when exceeded: free text
  (`system_prompt`, `input_context`, `agent_output`) up to 1,000,000
  characters; names, identifiers, and labels up to 2,048 characters; lists
  (`business_object_keys`, `artifact_cids`, `prev`, `derived_from`, `tools`)
  up to 1,000 items. The OpenAPI schema declares them. `llm_config`,
  `metadata`, and the contents of `tools` items are not bounded per field,
  only by the request body cap.
- Validation 422s on every route omit the echoed `input`, so an oversized
  field is not returned in the error response.
- The canonical encoding that CIDs and commit-log entry hashes are computed
  over is now RFC 8785 (JSON Canonicalization Scheme), so independent
  verifiers can reproduce hashes with a standard library. Numbers use
  ECMAScript form (`1.0` and `1` encode identically) and object keys sort by
  UTF-16 code units. The CID changes for content that holds an integral or
  exponent-form float (`1.0`, `-0.0`, `1e16`, `1e-7`) or an object key outside
  the Basic Multilingual Plane; other content keeps its CID. The commit-log
  entry hash function is unchanged for the same inputs, but an entry whose CID
  changes gets a new hash. Existing databases holding such content fail audit,
  so deploy against a fresh database.
- Content with integers outside +/-(2^53 - 1), non-string object keys, or
  lone surrogates returns 400 (lone surrogates previously returned 500). Send
  large integers, such as 64-bit seeds, as strings.
- Every node table has a `v` column (NOT NULL, default 1), and every node's
  hashed content now starts with `"v": <v>`. Because `v` is inside the hashed
  content, every CID changes. `nodes.to_node` picks the assembler by
  `(table, v)`; `_ASSEMBLERS` is append-only, so a new content shape gets a new
  `v` and a new assembler while old rows keep verifying. A row with a `v` that
  has no assembler raises `TypeError`, which verification reports as a
  malformed node. `v` is a storage and hashing detail: it is not in any
  request or response.
  - Breaking: `create_all` does not alter existing tables and old CIDs no
    longer match, so this ships in the format-1 release with a fresh database.
  - `tests/shared/test_golden_vectors.py` pins the exact CIDs and entry hashes
    of format 1; any later byte change fails it.

- The `commit_log` table has a unique constraint on `prev_hash`
  (`uq_commit_log_prev_hash`), so two entries cannot chain to the same
  predecessor. Appends are already serialized by an advisory lock; the
  constraint stops a fork even if a write path skips the lock, and a
  violation fails the write instead of retrying.
  - Breaking: `create_all` does not alter existing tables, so an existing
    database silently lacks the constraint. This ships in the format-1
    release with a fresh database. To add it by hand, run
    `ALTER TABLE commit_log ADD CONSTRAINT uq_commit_log_prev_hash UNIQUE (prev_hash)`;
    it succeeds only if the log has no fork.

### Fixed

- `GET /audit/` cross-checks stored nodes against the commit log. A stored node
  with no commit-log entry is reported per node as `unlogged_node` (and now also
  fails `GET /verify/{cid}/`); coverage reports a commit-log entry with no stored
  node as `missing_node`. Previously, the audit detected modified nodes but not
  inserted or deleted ones.
- The temporal-order check now fails closed: a node or link target with no
  commit-log observation is a finding (`unlogged_node`, `unobserved_link`)
  instead of a pass, and `GET /verify/{cid}/` no longer reports an unlogged node
  as valid.
- Every full-audit check and count uses one committed database snapshot.
  Concurrent changes are visible to a later audit. On SQLite with the default
  rollback journal, an audit can delay writer commits or cause lock timeouts.
- A corrupt structural column, such as a non-hex link list, no longer makes
  `GET /audit/` and `GET /verify/{cid}/` return 500. The node is reported as
  a `malformed_node` failure against the malformed node's own CID, even when
  it is reached through a link. The audit still fails on a column that cannot
  be loaded as its declared type, such as an unparseable datetime.
- Concurrent config activations for one agent could all chain onto the same
  head. Two first activations forked the chain, after which every read of
  that agent's activations failed. The activation head is now read under the
  commit lock, in both `PUT /agents/{uuid}/config/` and the
  `expected_head` compare-and-set in `POST /agents/{uuid}/activations/`.
- Node content that cannot be hashed returns 400 instead of 500. This
  covers JSON nested deeper than 64 levels (previously unbounded recursion
  inside the hash function) and non-finite floats such as `Infinity`. The
  64-level bound counts the canonical encoding's own two wrapper levels, so
  user metadata may nest 62 levels deep.
- Stored content that can no longer be encoded, such as metadata nested past
  that bound, is reported by `GET /verify/{cid}/` and `GET /audit/` as a
  `canonical_encoding_error` failure instead of aborting with 400. This
  applies to databases created before this change, which may hold honest
  nodes nested 63 or more levels deep; they now audit as
  `canonical_encoding_error`. There is no scan or migration: the change
  ships with the fresh-database release.
- A submission whose CID matches a stored node of the same type with different
  canonical bytes now answers 409 and logs one `CID collision on <cid>`
  warning, instead of silently returning the stored node. A cross-type
  collision is not separately checked: `type` is inside the hashed content, so
  it would also be a full SHA-256 collision.
- `make test-integration` runs pytest as `python -m pytest`, so the suite sees
  the client that `uv run --with` installs; before, it failed at import.

### Migration

There is none. Start this release on a fresh database; it refuses to start on
a database that has commit-log entries and no `ledger_format` row. Keep the
old deployment and database if historical verification is required. Set
`AUTH_SERVICE_KEYS` (or `AUTH_DISABLED=true` for local runs) and give every
caller a key: the ledger hook and `tools/aits_view.py` read `AITS_API_KEY`.

## 0.2.0

Breaking pre-1.0 lineage revision. The service and generated `aits-client`
metadata use 0.2.0; the client previously used 0.1.6.

### Changed

- Root LT CIDs identify lineage graphs. Membership derives only through
  `prev`; `session_uuid` is removed from stored LTs, hashes, and API models.
- Agents no longer hold a current-session pointer. Callers select roots and
  explicit parents; cross-graph inputs use exact LT CIDs in `derived_from`.
- `POST /lineage/` creates a root. `POST /lineage/{root_cid}/nodes/` handles
  structural nodes, existing DUT attachments, and atomic inline DUT creation.
- The generated client exposes `create_root`, `get_lineage_node`, and
  `create_lineage_node` for the new contract.

### Fixed

- Concurrent identical submissions deduplicate to one node while recording
  each observation, instead of failing with a uniqueness violation.
- Router CID inputs require exactly 64 hexadecimal characters of either case
  and reject whitespace, including artifact SHA-256 digest lookups; invalid
  values return 400.
- OpenAPI reports the service package version, 0.2.0.

### Migration

Follow [Migrate to root-LT lineage](docs/howto/migrate-to-root-lt-lineage.md).
Use a fresh database and retain the old deployment and database if historical
verification is required. Content deduplication does not provide provider-retry
idempotency.
