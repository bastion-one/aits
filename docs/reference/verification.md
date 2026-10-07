# Verification and audit reference

| HTTP operation | Generated `AuditApi` method | Result |
| --- | --- | --- |
| `GET /verify/{cid_hex}/` | `verify(cid_hex)` | `VerifyRead` |
| `GET /audit/` | `full_audit()` | `AuditRead` |
| `GET /commits/` | `commits(cid=..., after_seq=..., limit=...)` | List of `CommitEntryRead` |
| `GET /ledger/head` | `head()` | `LedgerHeadRead` |

These routes require [service authentication](configuration.md#authentication)
unless open mode is enabled.

## Checks and responses

Per-node verification checks the node's hash and recursively checks linked
nodes' structure. It then checks that the requested node has an observation
and that each direct link target was first observed no later than that node.
It does not verify the entire commit-log hash chain or recursively check
every ancestor's temporal order. Missing requested nodes return 404; malformed
CIDs return 400. A verification failure for a stored node returns 200 with
`valid: false`, `property_violated`, and `message`.

Full audit uses one committed snapshot to check every stored node, temporal
order, observed CIDs with no stored node, and the commit-log hash chain. Its
response contains `valid`, `nodes_checked`, `nodes_verified`, `commit_entries`,
and `failures`. Each failure identifies `record_kind`, `record_key`,
`property_violated`, and `message`. Counts describe that snapshot;
`nodes_verified` counts nodes passing both structural and temporal checks.

Commit reads are ordered by ascending `seq`. Optional `cid` filters by node;
optional `after_seq` returns only entries with a greater `seq`; the two
combine. `limit` defaults to 100 and is bounded to 1 through 1000; a value
outside that range returns 422. Entries contain `seq`, `cid`, `recorded_at`,
`prev_hash`, `entry_hash`, and `principal`. There is no offset parameter;
page by passing the last `seq` seen as `after_seq`. Repeated node submissions
can have multiple observations.

`GET /ledger/head` returns `ledger_id`, `seq`, and `entry_hash`: the first
entry's `entry_hash` as `ledger_id`, and the latest entry's `seq` and
`entry_hash`. All three are null on an empty log. The values are derived per
request; AITS stores no anchors. A verifier outside AITS can record the head,
page from its last recorded `seq` to the current head with `after_seq`, and
check that each entry's `prev_hash` equals its predecessor's `entry_hash`. See
[ledger format](ledger-format.md#commit-entry-hashes) for how `entry_hash` is
computed.

## Findings

| `property_violated` | Meaning |
| --- | --- |
| `cid_mismatch` | Stored content no longer hashes to the node's CID |
| `dangling_link` | A linked node is missing |
| `malformed_node` | Node assembly or link validation failed, including an unknown schema version |
| `canonical_encoding_error` | Stored content cannot be canonically encoded |
| `unlogged_node` | A stored node has no commit-log observation |
| `unobserved_link` | A direct link target has no commit-log observation |
| `temporal_order` | A link target was first observed after the referencing node |
| `missing_node` | A commit-log observation references a CID with no stored node; full audit only |
| `chain_broken` | A commit entry does not reference the expected predecessor hash; full audit only |
| `entry_hash_mismatch` | A commit entry's content does not match its stored entry hash; full audit only |

Verification reports the first failure encountered for each checked node,
not every possible failure. Rows that cannot be loaded as their declared types,
such as invalid stored datetimes, can still abort an audit.

Validity describes internal consistency, not complete capture of AI work or
independent proof that history was preserved. See the
[verification explanation](../explanation/architecture.md#verification) and
[ledger format](ledger-format.md).
