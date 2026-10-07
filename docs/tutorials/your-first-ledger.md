# Your first ledger

In this tutorial you'll record one agent's work in AITS, end to end: you'll
register an agent, record its configuration, hash a sample file, record
a sample inference result, and chain that call into a lineage graph you can
verify.
Then you'll tamper with a record behind AITS's back and watch verification
catch it.

By the end you will have a working `ledger.py` and a graph you can verify.

You don't need to know anything about content-addressing going in — you'll see
it work.

## Before you start

Requirements: Python 3.13+, uv, Docker with Compose, and a disposable copy
of this repository. Use a fresh checkout with no `.env.local`. The following
setup uses a dedicated container and volume on port 55432; port 8000 must also
be available. Do not point this exercise at a ledger you need to keep: Step 7
changes database rows directly.

From the disposable project's root:

```bash
make deps
cp .env.example .env
cat >> .env <<'EOF'
DATABASE_URL=postgresql+psycopg://bastion:bastion@127.0.0.1:55432/bastion
POSTGRES_PORT=55432
COMPOSE_PROJECT_NAME=aits-tutorial
EOF
docker compose run --rm -d --name aits-tutorial-postgres --service-ports --use-aliases postgres
```

Wait for PostgreSQL to accept connections:

```bash
until docker exec aits-tutorial-postgres pg_isready -U bastion -d bastion; do
  sleep 1
done
uv run --frozen fastapi dev
```

Leave the server running. In a second terminal, from the same project root:

```bash
make regen
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/ready
```

The checks return `{"status":"ok"}` and `{"status":"ready"}`.
This setup accepts unauthenticated requests for local learning.

Save the Python blocks below in `ledger.py`. After each numbered step through
Step 6, run the accumulated script:

```bash
uv run --frozen --with ./clients/python python ledger.py
```

Each run registers a new agent. Configuration and artifact records can be
shared through deduplication; the new agent starts with no activation, so
`expected_head=None` remains valid. Printed CIDs and UUIDs vary between runs.

## The scenario

You operate an agent that extracts line items from invoices. It just processed
invoice `INV-1007`. We will record sample configuration, input, and output
without calling an LLM. Verification will show whether stored contents match
their recorded CIDs and observations.

We'll build that record one object at a time. Create a file called `ledger.py`
and add each block as we go.

Start with the imports and a client:

```python
"""ledger.py — record and verify one agent's work in AITS."""

from aits_client import (
    ApiClient,
    Configuration,
    AgentsApi,
    ConfigsApi,
    ArtifactsApi,
    DutsApi,
    LineageApi,
    AuditApi,
    AgentCreate,
    ConfigCreate,
    ActivationCreate,
    DUTCreate,
    LineageRootCreate,
    LineageNodeCreate,
)

client = ApiClient(Configuration(host="http://127.0.0.1:8000"))

agents = AgentsApi(client)
configs = ConfigsApi(client)
artifacts = ArtifactsApi(client)
duts = DutsApi(client)
lineage = LineageApi(client)
audit = AuditApi(client)
```

A **CID** (content identifier) is the SHA-256 of a node's canonical content
and named links. It identifies the node throughout this exercise.

## Step 1 — Register the agent

Register the agent. Its genesis CID anchors later records; its display name
is stored outside the hash.

```python
agent = agents.register(AgentCreate(name="invoice-extractor"))
print("agent     ", agent.cid, agent.uuid)
```

Run `ledger.py`. You should see `agent` followed by a 64-character CID and
a UUID. Keep adding to the same file.

## Step 2 — Record its configuration

Record the system prompt, model settings, tools, and metadata reported for
the agent. A configuration has no agent link, so agents using identical
settings share one configuration record.

```python
cfg = configs.create(
    ConfigCreate(
        system_prompt="Extract line items from invoices as JSON.",
        llm_config={"model": "gpt-x", "temperature": 0.0},
        tools=[],
        metadata={"team": "finance"},
    )
)
print("config    ", cfg.cid)
```

Activate the recorded configuration for this agent:

```python
activation = configs.activate(
    agent.uuid,
    ActivationCreate(config_cid=cfg.cid, expected_head=None),
)
print("activation", activation.cid)
```

Run `ledger.py`. You should now see `agent`, `config`, and `activation` CIDs.
The activation declares the configuration the agent should use; each DUT
records the configuration the caller says it used.

## Step 3 — Hash the sample file

Hash the sample invoice and retain its hash record as an `Artifact`. AITS
does not retain the uploaded bytes:

```python
invoice = b"INV-1007\nWidget x4 @ 2.50\nGadget x1 @ 9.99\n"
artifact = artifacts.upload(invoice)
print("artifact  ", artifact.cid, "sha256:", artifact.sha256)
```

Run `ledger.py`. The `artifact` line contains both a node CID and the SHA-256
of the invoice bytes. The CID is what the DUT will link. Keep the file itself
in your own storage if you need to retrieve it later.

## Step 4 — Record the sample inference call

A `DataUniqueTag` (DUT) records a single call. Create one with the sample input
and output, linked to the agent, configuration, and artifact:

```python
dut = duts.create(
    DUTCreate(
        agent_uuid=agent.uuid,
        config_cid=cfg.cid,
        span_id="extract-INV-1007",
        business_object_keys=["invoice:INV-1007"],
        sequence=0,
        input_context=invoice.decode(),
        agent_output='[{"item": "Widget", "qty": 4}, {"item": "Gadget", "qty": 1}]',
        artifact_cids=[artifact.cid],
    )
)
print("dut       ", dut.cid)
```

Run `ledger.py`. You should see a `dut` CID after the artifact.
`business_object_keys` connects the record to the invoice in your own system.

## Step 5 — Chain it into a lineage graph

A single DUT records one call. A `LineageTag` (LT) graph records a
*process*: you create a root, then add one node per round of work. Each
node names its predecessor(s) in `prev` and the DUT it produced — and those
links are folded into the node's own CID.

```python
root = lineage.create_root(
    LineageRootCreate(
        actor_id="invoice-extractor",
        step_id="extract",
        transformation="root",
    )
)
step = lineage.create_lineage_node(
    root.cid,
    LineageNodeCreate(
        actor_id="invoice-extractor",
        step_id="extract",
        transformation="invoice-extraction",
        prev=[root.cid],
        dut_cid=dut.cid,
    ),
)
print("root      ", root.cid)
print("step      ", step.node.cid)
```

Run `ledger.py`. The new `root` and `step` CIDs identify the graph and its
work node. The step links the DUT and names the root as its parent.

## Step 6 — Verify it

One endpoint verifies any object: `GET /verify/{cid}/` recomputes the node's
CID from its stored contents, then follows every link and recomputes those
too, all the way down. Verifying the lineage step therefore *is* verifying
the DUT, the config, the agent genesis, and the artifact beneath it:

```python
print()
print("step valid:  ", audit.verify(step.node.cid).valid)
print("full audit:  ", audit.full_audit().valid)
print("observations:", len(audit.commits(cid=dut.cid)))
```

`full_audit` does the same walk over *everything* in the ledger, plus the
commit log — the append-only, hash-chained record of when the server observed
each submission. That log is where `recorded_at` lives: the ledger's own
clock, outside the node CID and inside the commit-entry hash.

Run the whole script:

```bash
uv run --frozen --with ./clients/python python ledger.py
```

```text
agent      0f64…cf21 6f1c2a9e-…
config     9b1f…e2a4
activation 5d08…113c
artifact   3a7c…d1f0 sha256: 8e51…77be
dut        b2e4…c8a0
root       c90d…77e1
step       41ad…9b6d

step valid:   True
full audit:   True
observations: 1
```

You have recorded an agent, its configuration, a file hash, a sample call,
and a lineage graph tying them together. **Copy
the `dut` and `step` CIDs from your output** — you'll need them next.

## Step 7 — Watch it catch a tampered record

Now change the stored DUT and observe the verification failure. The API won't let you edit a recorded DUT — that's the whole
design — so we'll go around it and change the stored output directly in
Postgres, the way a botched migration or a bad actor with database access
might.

Use the `dut` CID from your run:

```bash
docker exec aits-tutorial-postgres psql -U bastion -d bastion -c \
  "UPDATE dut SET agent_output = '[{\"item\": \"Widget\", \"qty\": 400}]' WHERE cid = decode('<your-dut-cid>', 'hex');"
```

```text
UPDATE 1
```

The row now claims a quantity of 400. Ask AITS to verify it — over HTTP this
time, so you can see the raw report:

```bash
curl -s http://127.0.0.1:8000/verify/<your-dut-cid>/
```

```json
{
  "cid": "<your-dut-cid>",
  "valid": false,
  "property_violated": "cid_mismatch",
  "message": "tampered: node at <your-dut-cid> no longer hashes to its address (recomputed …)"
}
```

The stored contents no longer hash to the address the row lives at. And
because verification is recursive, the lineage step fails too — verify
`<your-step-cid>` and you'll get the same report: the walk reached the
tampered DUT. The current contents no longer match the recorded CID.

### "But I have database access — can't I just fix the hash too?"

You can try. The CID is computed from the contents by a known algorithm, so
anyone who can write the row can recompute it — using the app's own code, even.
That's exactly what a determined attacker would do, so let's be that attacker.
Create `forge.py`:

```python
"""forge.py — move a tampered DUT to its recomputed CID, the way an attacker
with database access would. Its hash matches, but its observation is missing."""

import sys

from sqlmodel import Session

from app.dag import compute_cid
from app.db import engine
from app.models import DataUniqueTag
from app.nodes import to_node

old_cid = bytes.fromhex(sys.argv[1])

with Session(engine) as session:
    dut = session.get(DataUniqueTag, old_cid)
    content, links = to_node(dut)        # assembled from the tampered contents
    dut.cid = compute_cid(content, links)
    session.add(dut)
    session.commit()
    print("forged cid:", dut.cid.hex())
```

Run it against your DUT:

```bash
PYTHONPATH=. uv run --frozen python forge.py <your-dut-cid>
```

Verify the forged CID it printed:

```bash
curl -s http://127.0.0.1:8000/verify/<forged-cid>/
```

```json
{
  "cid": "<forged-cid>",
  "valid": false,
  "property_violated": "unlogged_node",
  "message": "node <forged-cid> has no commit-log observation"
}
```

The forged record passes the structural hash check, but the endpoint also
checks for a commit-log observation. The new CID has none, so verification
fails with `unlogged_node`. The record also moved away from the CID its
lineage step references. Verify that step:

```bash
curl -s http://127.0.0.1:8000/verify/<your-step-cid>/
```

```json
{
  "cid": "<your-step-cid>",
  "valid": false,
  "property_violated": "dangling_link",
  "message": "dangling link: no node found at CID <your-dut-cid>"
}
```

The step still references the original DUT CID, where no row remains. A full
audit also reports `missing_node` for the original CID's observation and
`unlogged_node` for the forged record.

These checks detect the changes made in this exercise. They do not prove that
a privileged actor never rewrote the entire ledger and observation history.
Retained CIDs or log hashes outside AITS can provide evidence against such a
rewrite. See [verification limits](../explanation/architecture.md#verification).

## What you built

```text
Agent genesis ◀──agent──┐                      ┌──artifacts──▶ Artifact
(cid; name off-hash)    │                      │               (cid + sha256)
                        ├────────  DUT  ───────┤
AgentConfig   ◀──config─┘         (cid)
(cid; behavioral-only)              ▲
                                    │ dut
                  LT step (cid) ────┘
                        │ prev
                        ▼
                  LT root (cid)
```

Each arrow is a named edge included in the CID of the node it leaves from.
Changing a linked node makes verification of the step fail.

## Where to go next

Stop the development server with Ctrl+C. Remove only this exercise's container
and volume from the disposable project root:

```bash
docker stop aits-tutorial-postgres
docker compose down -v
```

- **Record a call atomically** — [Record an inference and lineage node](../howto/record-inline-dut.md).
- **Look up verification findings** — [Verification and audit](../reference/verification.md).
- **Look up exact contracts** — see the [lineage reference](../reference/lineage.md)
  and [OpenAPI schema](../../openapi.json).
- **Understand the design** — [Architecture](../explanation/architecture.md)
  explains content addressing, graph membership, provenance, and the two clocks.
- **Update an existing integration** — see
  [Migrate to root-LT lineage](../howto/migrate-to-root-lt-lineage.md).
