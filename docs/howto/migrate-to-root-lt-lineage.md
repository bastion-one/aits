# Migrate to root-LT lineage

Use this guide to update a session-UUID integration to the current **0.3.0**
service and generated client. Root-LT lineage was introduced in 0.2.0;
0.3.0 adds format 1 and service authentication. Integrations already using
root LTs still need the fresh database and authentication steps below.

This is a breaking lineage model revision. There is no mechanical
UUID-to-CID conversion and no safe in-place removal of the old LT hash
field. Deploy against a fresh database. Keep an archived copy of the old
deployment and database if you still need to verify historical records.

Do not reset a live database as part of this upgrade. Historical conversion
tooling and Alembic adoption are separate work.

## Prepare the 0.3.0 deployment

1. Retain the old application, matching client, and database for historical
   verification. Do not overwrite or convert that ledger in place.
2. Provision a fresh database and point the new service's `DATABASE_URL` at it.
   Startup creates the tables and format-1 record; `create_all()` does not
   upgrade old tables or hashes.
3. Set `AUTH_SERVICE_KEYS` and remove `AUTH_DISABLED` (or set it to `false`).
   Give each caller its service secret. For local testing only, open mode is
   available through `AUTH_DISABLED=true`.
4. Run `make regen` and `make build-client` from the 0.3.0 source. Install the
   generated 0.3.0 client in downstream integrations. Configure their SDK with
   `Configuration(host=..., access_token=...)`; the viewer and Claude hook
   use `AITS_API_KEY`.

The [configuration reference](../reference/configuration.md) lists shared limits.
Large integers in metadata must be strings under the new canonical encoding.

## API mapping

| Old usage | New usage |
| --- | --- |
| Session UUID creation/selection | Create a root and retain its returned CID |
| `AppendCreate.dut` CID | `LineageNodeCreate.dut_cid` |
| Flat `RecordCreate` DUT fields | Nested inline `dut` input |
| Omitted parents/frontier selection | Explicit nonempty `prev` |
| `used_sessions` | Exact consumed LT CIDs in `derived_from` |
| Agent current-session record/rotation | Caller-owned root selection and node creation |
| Session summaries | Root CID summaries; agent participation read remains available |

## Client methods

Use the generated client's `LineageRootCreate` and `LineageNodeCreate`
request objects. See the [lineage reference](../reference/lineage.md#operations)
for exact method signatures and responses.

Removed: `start_session`, `append`, `record`, `AgentsApi.record`,
`AgentsApi.new_session`, and any `session_uuid` / `used_sessions` fields.
`GET /agents/{agent_uuid}/sessions/` still lists graphs the agent produced
work in; each summary identifies the graph by root CID.

## Root plus inline node

With a configured `ApiClient` named `client` and a registered `agent` that
has an active configuration:

```python
from aits_client import InlineDUTCreate, LineageApi, LineageNodeCreate, LineageRootCreate
from aits_client import AuditApi

lineage = LineageApi(client)
root = lineage.create_root(
    LineageRootCreate(actor_id=agent.name, step_id="s", transformation="root")
)
created = lineage.create_lineage_node(
    root.cid,
    LineageNodeCreate(
        actor_id=agent.name,
        step_id="s",
        transformation="work",
        prev=[root.cid],
        dut=InlineDUTCreate(
            agent_uuid=agent.uuid,
            span_id="r1",
            input_context="prompt",
            agent_output="completion",
        ),
    ),
)
assert created.root == root.cid
assert created.node.root == root.cid
assert AuditApi(client).verify(created.node.cid).valid
```

Identical root content and event time dedupe to one CID. Retain the returned
root if you will write more nodes.

## Handle errors and retries

Update error handling to match the [lineage error contract](../reference/lineage.md#cid-validation-and-errors).
In particular, supply explicit nonempty parents and remove legacy fields;
unknown fields now fail schema validation. CID fields reject whitespace,
including space-separated hex that earlier parsers accepted.

Replaying identical node content adds an observation, but does not guarantee
that retrying an ambiguous inference request yields its prior LT. See
[deduplication](../reference/lineage.md#deduplication) before choosing a retry policy.

## Verify the updated integration

Run the example above with your configured client and registered agent. Both
root assertions and verification must pass. Read the graph back and confirm
that the created node appears under the retained root CID. On a fresh ledger,
`AuditApi(client).full_audit().valid` must also be true.

In authenticated mode, check that a resource request without a service key
returns 401 and that new commit entries name the caller's service. Page agent
lists with `limit` and `offset`; the current default returns only 100 agents.
Keep retry policy explicit: node deduplication does not supply provider-call
idempotency.
