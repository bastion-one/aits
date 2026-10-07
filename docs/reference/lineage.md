# Lineage API reference

Lineage graphs are identified by their root Lineage Tag (LT) CID. This page
covers the current root-LT contract. See [OpenAPI](../../openapi.json) for
complete schemas and the running service's `/docs` for interactive API docs.
For rationale, see [Architecture](../explanation/architecture.md). To update
an older client, see [Migrate to root-LT lineage](../howto/migrate-to-root-lt-lineage.md).

## Operations

Methods below belong to the generated Python SDK's `LineageApi`.

| HTTP operation | SDK method | Response |
| --- | --- | --- |
| `POST /lineage/` | `create_root(lineage_root_create)` | `LineageRead` |
| `POST /lineage/{root_cid}/nodes/` | `create_lineage_node(root_cid, lineage_node_create)` | `LineageNodeRead` |
| `GET /lineage/nodes/{lt_cid}/` | `get_lineage_node(lt_cid)` | `LineageRead` |
| `GET /lineage/{root_cid}/` | `get_lineage_graph(root_cid)` | `LineageGraphRead` |
| `GET /lineage/` | `list_roots()` | List of `LineageGraphSummary`; optional `limit`, default 100 |
| `GET /lineage/nodes/{lt_cid}/watermark/` | `watermark(lt_cid)` | Node CID and rendered watermark |
| `GET /lineage/nodes/{lt_cid}/traceback/` | `trace_back(lt_cid)` | Contributing DUTs with agent/config attribution, ordered by event time |

Both creation operations return `201`. Node creation returns a `Location`
header containing `/lineage/nodes/{node.cid}/`.

`GET /agents/{agent_uuid}/sessions/` (`AgentsApi.list_sessions`) returns
root-based graph summaries for graphs containing a DUT produced by that agent.
An LT's `actor_id` label alone does not establish participation.

## Requests

`LineageRootCreate` requires `actor_id`, `step_id`, and `transformation`.
Optional `occurred_at` defaults to server time. A root has no parents or DUT.

`LineageNodeCreate` takes the same metadata plus:

| Field | Contract |
| --- | --- |
| `prev` | Required, nonempty list of LT CIDs; every parent must resolve to the path root |
| `derived_from` | Exact LT CIDs from other roots; defaults to an empty list |
| `dut_cid` | Existing DUT CID, or null |
| `dut` | `InlineDUTCreate` object, or null |

Non-null `dut_cid` and `dut` are mutually exclusive. Omit both for a
structural node. The path root must identify an LT with no `prev` links.
Parent lists are deduplicated and sorted in stored links. The server does
not choose parents from the frontier.

`InlineDUTCreate` requires `agent_uuid`, `span_id`, `input_context`, and
`agent_output`. Its defaults are:

- `config_cid`: the agent's active configuration; an explicit existing
  configuration overrides it.
- `occurred_at`: the enclosing LT's resolved event time.
- `sequence`: 0.
- `business_object_keys` and `artifact_cids`: empty lists.

The referenced agent, configuration, and artifacts must exist. Inline creation
commits the DUT, LT, and both observation entries in one transaction. A failed
transaction leaves neither new node nor observation behind. `POST /duts/`
remains available for standalone DUT creation.

All three request models reject unknown fields. Supplied event times must
include a timezone; omitted or null times use the defaults above.

## Responses and graph reads

`LineageRead` contains `cid`, derived `root`, the node metadata, `prev`,
`derived_from`, and `dut` (a CID or null). A root's `cid` equals its `root`.

`LineageNodeRead` contains `{root, node, dut}`. Here the envelope's `dut` is
the full DUT body or null; `node.dut` is only its CID or null. The envelope's
`root` equals both the path root and `node.root`.

`LineageGraphRead` contains `{root, nodes, frontier}`. Membership follows
only `prev` ancestry. Nodes are ordered by event time, then CID. The frontier
contains graph nodes that no other graph node references through `prev`.
Traceback follows both `prev` and `derived_from` and collects attached DUTs.

`LineageGraphSummary` contains `root`, `nodes` (count), `started_at`,
`last_occurred_at`, and `frontier`. Times are the minimum and maximum node
event times. Summaries sort by latest event time descending, then root CID
descending to break ties.

## CID validation and errors

Lineage CID inputs require exactly 64 hexadecimal characters, without
whitespace. All CID inputs accept either letter case. Returned CIDs are
lowercase.

| Condition | Status |
| --- | --- |
| Malformed CID | 400 |
| Referenced LT or DUT is absent or the wrong kind | 404 |
| Referenced inline agent, configuration, or artifact is absent | 404 |
| Path root is not a root LT | 400 |
| A `prev` LT has another root, or its ancestry resolves to multiple roots | 400 |
| A `derived_from` LT has the target root | 400 |
| Both `dut_cid` and `dut` are non-null | 400 |
| Supplied event time has no timezone | 400 |
| Inline config is omitted and the agent has no active configuration | 409 |
| Schema errors, including missing/empty `prev` or unknown fields | 422 |

Shared failures also apply: missing or incorrect service keys return 401;
forward-dated events and unencodable canonical content return 400; oversized
request bodies return 413; field/list limits return 422; a CID collision
returns 409; and commit-lock timeout returns 503 with `Retry-After: 1`.
See [configuration and limits](configuration.md).

## Deduplication

Identical node content, links, and event time return the existing CID, with a
new commit-log observation for each submission. Concurrent identical writes
are serialized in PostgreSQL. Omitting event time can produce different CIDs
on separate requests because the default is server time.

Content deduplication does not guarantee that retrying an ambiguous provider
inference request returns the prior inference LT. AITS supplies no automatic
provider retry policy or cross-system idempotency key.
