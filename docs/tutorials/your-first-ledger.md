# Your first ledger

In this tutorial you'll record one agent's work in AITS, end to end: you'll
register an agent, pin down its configuration, capture the file it read, stamp
the inference call it made, and chain that call into a session you can verify.
Then you'll tamper with a record behind AITS's back and watch verification
catch it.

By the end you'll have a working `ledger.py` and a feel for the five objects
and why each one's identity *is* its hash.

You don't need to know anything about content-addressing going in — you'll see
it work.

## Before you start

You'll need the service running and the Python SDK built. From the project root:

```bash
make deps                     # install dependencies from uv.lock
cp .env.example .env          # defaults match the dev Postgres
make dev                      # boot Postgres + start FastAPI on :8000
make regen                    # in a second terminal: build the Python SDK
```

Leave `make dev` running. Check it's up:

```bash
curl -s http://127.0.0.1:8000/health
```

```json
{"status":"ok"}
```

Every command below runs from the project root, and every Python snippet runs
with the freshly-built SDK on the path:

```bash
uv run --with ./clients/python python ledger.py
```

## The scenario

You operate an agent that extracts line items from invoices. It just processed
invoice `INV-1007`. You want a record of exactly *what the agent was* and *what
it produced* — one you can hand to an auditor a year from now and prove was
never edited.

We'll build that record one object at a time. Create a file called `ledger.py`
and add each block as we go; you'll run the whole thing once at the end.

Start with the imports and a client:

```python
"""ledger.py — record and verify one agent's work in AITS."""

from bastion_one_client import (
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
    SessionCreate,
    AppendCreate,
)

client = ApiClient(Configuration(host="http://127.0.0.1:8000"))

agents = AgentsApi(client)
configs = ConfigsApi(client)
artifacts = ArtifactsApi(client)
duts = DutsApi(client)
lineage = LineageApi(client)
audit = AuditApi(client)
```

One term you'll see on every object below: **CID** (content identifier). It's
the SHA-256 of the object's canonical contents *including the CIDs of the
objects it links*, and it is the object's primary key. There's no separate
"checksum" field to compare against — an object either lives at the address
its contents hash to, or it doesn't.

## Step 1 — Register the agent

Creating an agent records its **genesis node** — `{uuid, created_at}` hashed
into a CID. That CID is the permanent anchor everything else points at. The
`name` is the one mutable thing in sight: a display label stored *outside* the
hash, so you can rename the agent later without disturbing a single record
below it.

```python
agent = agents.register(AgentCreate(name="invoice-extractor"))
print("agent     ", agent.cid, agent.uuid)
```

(`register` can also take the agent's starting config inline and activate it
in the same call — we'll use that in [the fast path](#the-fast-path) at the
end. Here we take the long way round so each object earns its keep.)

## Step 2 — Record its configuration

The configuration is what the agent actually *was* at inference time: its
system prompt, model settings, tools, and metadata. Note what's absent: the
agent. A configuration is **behavioral-only** — a recipe, not a binding — so
two agents running the same recipe share one record.

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

Because the CID *is* the identity, recording the same configuration twice is
idempotent — the second call returns this same `cid` instead of creating a
duplicate. Change a single character of the prompt and you get a different
CID, and so a different configuration. That's the point: there's no such thing
as "the config, edited."

Since the config no longer names the agent, declaring *which* config an agent
is supposed to be running is its own record — an activation:

```python
activation = configs.activate(
    agent.uuid,
    ActivationCreate(config_cid=cfg.cid, expected_head=None),
)
print("activation", activation.cid)
```

Activations form an append-only chain per agent (`expected_head` is your claim
about the current chain head; a stale claim is rejected). The chain records
what *should* be running; each inference call will separately record what
*did* run — that gap is what drift detection reads.

## Step 3 — Capture the file it read

The agent read an invoice. Store its bytes as an `Artifact`:

```python
invoice = b"INV-1007\nWidget x4 @ 2.50\nGadget x1 @ 9.99\n"
artifact = artifacts.upload(invoice)
print("artifact  ", artifact.cid, "sha256:", artifact.sha256)
```

An artifact answers to two hashes. `sha256` is the digest of the raw bytes —
re-hash a file you're holding and ask "has AITS seen this?" via
`GET /artifacts/by-digest/{sha256}/`. `cid` is the artifact's node identity,
the thing other records link. Upload is idempotent on the bytes, and AITS
keeps only the hashes — the bytes themselves are dropped after hashing.

## Step 4 — Stamp the inference call

Now the heart of it. A `DataUniqueTag` (DUT) is the immutable record of one
inference call — one *span*, in tracing terms. Its content carries the text
roundtrip (`input_context`, `agent_output`), and its links pin **who** ran
(`agent_uuid` resolves to the agent's genesis CID), **as what** (`config_cid`),
and **on which bytes** (`artifact_cids`). All of it — content, links, and the
link *names* — folds into the DUT's CID, so editing any field or quietly
de-linking the artifact changes what the record hashes to.

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

`business_object_keys` is your hook back into your own world — here, the
invoice the work was about. `span_id` is the runtime handle your
instrumentation assigned when the call started, before its CID could exist;
it also keeps two otherwise-identical calls distinct. Submitting the exact
same record twice is a replay: it dedupes to one node, but the ledger's commit
log still gains one entry per submission, so every observation stays counted.

## Step 5 — Chain it into a session

A single DUT records one call. A `LineageTag` (LT) session records a
*process*: you open the session, then append one node per round of work. Each
append names its predecessor(s) in `prev` and the DUT it produced — and those
links are folded into the append's own CID.

```python
session = lineage.start_session(
    SessionCreate(actor_id="invoice-extractor", step_id="extract")
)
step = lineage.append(
    session.session_uuid,
    AppendCreate(
        actor_id="invoice-extractor",
        step_id="extract",
        transformation="invoice-extraction",
        prev=[session.cid],
        dut=dut.cid,
    ),
)
print("session   ", session.session_uuid)
print("step      ", step.cid)
```

Because the append links the DUT's *CID*, it pins the DUT exactly as it stood
— a frozen snapshot by construction, with no separate snapshot field to
maintain. Sessions are DAGs, not lists: parallel tool calls fork (two appends
sharing a `prev`), synthesis joins (one append with two `prev` entries), and a
supervisor consuming another agent's session links it in `derived_from`. The
session's current tips are its **frontier**, returned by
`GET /lineage/{session_uuid}/`.

## Step 6 — Verify it

One endpoint verifies any object: `GET /verify/{cid}/` recomputes the node's
CID from its stored contents, then follows every link and recomputes those
too, all the way down. Verifying the lineage step therefore *is* verifying
the DUT, the config, the agent genesis, and the artifact beneath it:

```python
print()
print("step valid:  ", audit.verify(step.cid).valid)
print("full audit:  ", audit.full_audit().valid)
print("observations:", len(audit.commits(cid=dut.cid)))
```

`full_audit` does the same walk over *everything* in the ledger, plus the
commit log — the append-only, hash-chained record of when the server observed
each submission. That log is where `recorded_at` lives: the ledger's own
clock, outside every CID, so a client can't pre-bake it.

Run the whole script:

```bash
uv run --with ./clients/python python ledger.py
```

```text
agent      0f64…cf21 6f1c2a9e-…
config     9b1f…e2a4
activation 5d08…113c
artifact   3a7c…d1f0 sha256: 8e51…77be
dut        b2e4…c8a0
session    c90d77e1-…
step       41ad…9b6d

step valid:   True
full audit:   True
observations: 1
```

That's a complete ledger: an agent, the exact configuration it ran, the file
it read, the call it made, and a verifiable chain tying them together. **Copy
the `dut` and `step` CIDs from your output** — you'll need them next.

## Step 7 — Watch it catch a tampered record

Verification only earns its keep when something is wrong, so let's make
something wrong. The API won't let you edit a recorded DUT — that's the whole
design — so we'll go around it and change the stored output directly in
Postgres, the way a botched migration or a bad actor with database access
might.

Use the `dut` CID from your run:

```bash
docker compose exec postgres psql -U bastion -d bastion -c \
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
tampered DUT. Nobody had to know what the original output *was* — the hash
alone proves the row changed.

### "But I have database access — can't I just fix the hash too?"

You can try. The CID is computed from the contents by a known algorithm, so
anyone who can write the row can recompute it — using the app's own code, even.
That's exactly what a determined attacker would do, so let's be that attacker.
Create `forge.py`:

```python
"""forge.py — move a tampered DUT to its recomputed CID, the way an attacker
with database access would, so the row verifies against itself again."""

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
PYTHONPATH=. uv run python forge.py <your-dut-cid>
```

Verify the forged CID it printed:

```bash
curl -s http://127.0.0.1:8000/verify/<forged-cid>/
```

```json
{ "cid": "…", "valid": true, "property_violated": null, "message": null }
```

The forged record verifies against itself — it's internally consistent. But
notice what fixing the hash *meant*: because the hash is the address, the
record **moved**. Nothing else moved with it. Verify the lineage step:

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

The step still names the *original* DUT CID — frozen into the step's own hash
back in Step 5 — and nothing lives at that address anymore. To silence this,
the attacker must rewrite the step's link, which changes the step's contents,
which moves the *step's* CID, which dangles whatever names the step — every
later round, any other agent's session that merged this one. And two more
tells remain: the commit log has an entry for the original CID and none for
the forged one, and the moment any of these hashes left AITS — an auditor
noted one, another system referenced one — there's a copy the attacker can't
reach.

That's what content-addressing buys you. It doesn't make a privileged edit
*impossible*; it makes a *quiet, local* one impossible — tampering is forced
to be total and detectable instead of small and silent.

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
                  LT session genesis (cid)
```

Each arrow is a named edge folded into the CID of the node it leaves from —
which is why an edit anywhere downstream surfaces at verification, and why
"fixing" a record can only ever move it somewhere nothing points.

## The fast path

Everything above used one primitive call per object, because the point was to
see each hash earn its keep. Day to day, the **agent is the entry point**: you
register it once, then record each round of its work against it — no session
bookkeeping, no repeated identity. The whole scenario again:

```python
"""fastpath.py — the agent is the entry point: register it, then record."""

from bastion_one_client import (
    ApiClient,
    Configuration,
    AgentsApi,
    AuditApi,
    AgentCreate,
    AgentRecordCreate,
    ConfigCreate,
)

client = ApiClient(Configuration(host="http://127.0.0.1:8000"))
agents, audit = AgentsApi(client), AuditApi(client)

# register: agent + config + activation, one call
agent = agents.register(
    AgentCreate(
        name="invoice-extractor",
        config=ConfigCreate(
            system_prompt="Extract line items from invoices as JSON.",
            llm_config={"model": "gpt-x", "temperature": 0.0},
        ),
    )
)
print("agent ", agent.uuid, "running", agent.active_config_cid[:12])

# record: one call per round -- the session starts itself, the config is the
# agent's active one, actor/step default to the agent's identity
round1 = agents.record(
    agent.uuid,
    AgentRecordCreate(
        span_id="extract-INV-1007",
        input_context="INV-1007\nWidget x4 @ 2.50\nGadget x1 @ 9.99\n",
        agent_output='[{"item": "Widget", "qty": 4}, {"item": "Gadget", "qty": 1}]',
        transformation="invoice-extraction",
    ),
)
print("dut   ", round1.dut.cid[:12], "| node", round1.node.cid[:12])
print("verify", audit.verify(round1.node.cid).valid)
```

The opinions baked in: `record` writes the DUT span *and* its lineage node in
one submission sequence, defaults the config to the agent's **active** one (so
"did" matches "should" unless you say otherwise), chains onto the agent's
**current session** — auto-started on first use, rotated with
`agents.new_session(agent.uuid, ...)` when a new piece of work begins — and
defaults `actor_id`/`step_id` to the agent's name and the span id. Pass
`artifact_cids=[...]` to link uploaded files, or `config_cid=...` to override.

Two escalations when you need more control: when one agent consumes another's
results, add `used_sessions=[session_a, session_b]` and each listed session's
frontier is linked in `derived_from`, so traceback from the combined answer
reaches into every contributor's work. And when one agent runs parallel
operations, skip the current-session pointer and use the session-scoped form
(`lineage.record(session_uuid, RecordCreate(...))`) with explicit sessions.
Routine config rollouts get the same one-call treatment:
`PUT /agents/{uuid}/config/` (`configs.set_config`) records and activates a
config without the CAS handshake.

## Where to go next

- **Reset and start clean** — `make db-reset` drops the Postgres volume, so your
  next run begins from an empty ledger.
- **Watch the observation log** — `GET /commits/` shows the hash-chained commit
  log; submit the same DUT twice and watch one node gain two entries.
- **Look up exact contracts** — the [reference](../reference/) documents the
  API surface; `openapi.json` is the generated spec.
- **Understand the design** — the model (CIDs, named edges, sessions as DAGs,
  the two clocks) is worked out in `docs/design/dag-lineage-tracing-model.md`
  and the `docs/step1`–`step5` notebooks (on the `howitworks` branch until
  merged).
