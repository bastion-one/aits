# AITS

AITS records what an AI agent was configured as and what it produced, in a form
that can later be verified as unaltered. Every configuration and inference record
is content-addressed: its identity is the SHA-256 of its canonical contents, so an
after-the-fact edit is detectable by recomputing the hash.

`aits` is the reference implementation: a [FastAPI](https://fastapi.tiangolo.com/)
service backed by Postgres, with a generated Python SDK.

## The data model

Five objects, each building on the one before it:

| Object | What it is | Identity |
|--------|------------|----------|
| Agent | Mutable identity — `(uuid, name)`. Registered first. | `cid` over its genesis node; `uuid` is the stable API handle |
| AgentConfig | Immutable configuration (`system_prompt`, `llm_config`, `tools`, `metadata`). | `cid` = SHA-256 of canonical content |
| DataUniqueTag (DUT) | Immutable snapshot of one inference call, pinned to the agent and config that produced it. | `cid` over content plus named links |
| LineageTag (LT) | A session DAG node linking DUTs into a traceable process. | `cid` over content plus named links; grouped by `session_uuid` |
| Artifact | A content-addressed binary object (a file an agent read or produced). | `cid` over the artifact node; `sha256` over the raw bytes |

Two consequences of content-addressing apply from the first call:

- Idempotency — recording the same `AgentConfig`, or uploading the same artifact
  bytes, twice returns the existing row instead of creating a duplicate.
- Verifiability — `GET /verify/{cid}/` recomputes a node's hash and recursively
  checks its links, while `GET /audit/` walks the whole ledger and commit log.

The [Your first ledger](docs/tutorials/your-first-ledger.md) tutorial walks
through canonical JSON, content-addressing, lineage, and tamper detection. See
[Architecture](docs/explanation/architecture.md) for the system design.

## Quickstart

Requirements: Python 3.13+, [uv](https://docs.astral.sh/uv/), and Docker (for the
development Postgres container).

```bash
make deps                     # install dependencies from uv.lock
cp .env.example .env          # adjust DATABASE_URL if needed
make dev                      # boot Postgres + start FastAPI (auto-reload)
```

The service runs at <http://127.0.0.1:8000>:

- Interactive API docs: <http://127.0.0.1:8000/docs>
- OpenAPI schema: <http://127.0.0.1:8000/openapi.json>
- Health check: <http://127.0.0.1:8000/health>

## Docker image

Build the server image from the project root:

```bash
docker build -t aits .
```

The image contains only the FastAPI service. It does not include Postgres or bake
in database credentials. Put the runtime database URL in an env file:

```bash
cp .env.example .env
# Edit DATABASE_URL to point at the database this container should use.
```

Then pass that file to Docker when starting the server:

```bash
docker run --rm --env-file .env -p 8000:8000 aits
```

If you are connecting to the Postgres container from this repo's
`docker-compose.yml`, put the AITS container on the same Docker network and set
`DATABASE_URL` to the Postgres container name. From inside the AITS container,
`127.0.0.1` means the AITS container itself, not the Postgres container.

```env
DATABASE_URL=postgresql+psycopg://bastion:bastion@bastion-one-postgres:5432/bastion
```

```bash
docker run --rm \
  --network aits_default \
  --env-file .env \
  -p 8000:8000 \
  aits
```

Downstream Docker Compose projects should use the same pattern with `env_file`:

```yaml
services:
  aits:
    image: aits
    env_file:
      - .env
    ports:
      - "8000:8000"
```

Compose also reads `.env` for `${VAR}` substitution in compose files; `env_file`
is what passes those variables into the AITS container.

## Security and support

This repository is a reference implementation of the Bastion-One model. It keeps
the API surface intentionally small and leaves API authentication, authorization,
network placement, and production deployment controls to the implementer.

For commercial support or the more comprehensive enterprise product, contact
Bastion-One.

## Using the API

AITS ships a generated Python SDK (`bastion-one-client`). Build it with
`make regen`, then the core flow looks like this:

```python
from bastion_one_client import ApiClient, Configuration, AgentsApi, AgentCreate, ConfigCreate

with ApiClient(Configuration(host="http://127.0.0.1:8000")) as client:
    agents = AgentsApi(client)

    # Register an agent and activate its starting config in one call.
    agent = agents.register(
        AgentCreate(
            name="invoice-extractor",
            config=ConfigCreate(
                system_prompt="Extract line items from invoices.",
                llm_config={"model": "gpt-x", "temperature": 0.0},
                tools=[],
                metadata={"team": "finance"},
            ),
        )
    )

    print(agent.uuid, agent.active_config_cid)
```

From `agent.active_config_cid` you can stamp inference calls as DUTs and chain
them into a lineage session. The full walkthrough — DUTs, artifacts, lineage,
and verification — is in [Your first ledger](docs/tutorials/your-first-ledger.md).

## Generated client

The checked-in `clients/build/` files are the source of truth for generating the
Python SDK; `clients/python/` is ephemeral and gitignored. Use:

```bash
make regen         # refresh openapi.json and regenerate clients/python/
make build-client  # build the generated SDK sdist + wheel
```

Run client builds through `make build-client`, not a top-level `uv build`.

## Development

Run `make help` for the full target list. Common tasks:

```bash
make deps          # install dependencies from uv.lock
make test          # unit tests (fast, sqlite)
make check         # formatting + pylint
make snyk          # dependency + SAST scans (requires Snyk CLI)
```

After changing dependencies in `pyproject.toml`, refresh the lockfile:

```bash
make lock
```

If a package was published inside the lockfile's `exclude-newer-span` window (currently
7 days), pass one-off CLI overrides on the lock command:

```bash
make lock UV_LOCK_FLAGS='--upgrade-package starlette --exclude-newer-package starlette=false'
```

## Claude Code integration POC

The `.claude/` directory is included as a proof-of-concept integration that
records Claude Code activity into a local AITS ledger. The runtime state under
`.claude/aits/` is gitignored; the tracked files are the hook and settings used
to demonstrate the integration.

## Documentation

- [Tutorials](docs/tutorials/) — start with [Your first ledger](docs/tutorials/your-first-ledger.md).
- [Explanation](docs/explanation/) — design rationale, starting with
  [Architecture](docs/explanation/architecture.md).

## Project layout

```
app/                  FastAPI application
  main.py             app factory + router wiring
  models.py           SQLModel tables
  dag.py              CID computation and DAG verification
  nodes.py            canonical node assembly from typed rows
  ledger.py           write path, commit log, and derived ledger reads
  routers/            HTTP endpoints, one module per object
clients/build/        generator inputs for the Python SDK
clients/python/       generated SDK (ephemeral — built by `make regen`)
docs/                 tutorials and explanation notes
tests/                unit suite (sqlite) + opt-in integration suite (SDK + server)
.claude/              Claude Code integration proof of concept
```

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).
