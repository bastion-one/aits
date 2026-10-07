# AITS

AITS records the configuration and results reported by an AI agent's caller,
in a form that can later be checked for consistency. Every configuration and
inference record is content-addressed: its identity is the SHA-256 of its
canonical contents, so a
change to stored content is detectable against its retained CID. See
[verification limits](docs/explanation/architecture.md#verification).

`aits` is the reference implementation: a [FastAPI](https://fastapi.tiangolo.com/)
service backed by Postgres, with a generated Python SDK.

## The data model

The main objects are:

| Object | What it is | Identity |
|--------|------------|----------|
| Agent | Stable identity with a mutable display name. Registered first. | `cid` over its genesis node; `uuid` is the stable API handle |
| AgentConfig | Immutable configuration (`system_prompt`, `llm_config`, `tools`, `metadata`). | `cid` = SHA-256 of canonical content |
| ConfigActivation | Append-only declaration of the configuration an agent should use. | `cid` over content and agent/config/previous-activation links |
| DataUniqueTag (DUT) | Immutable snapshot of one inference call, pinned to the agent and config that produced it. | `cid` over content plus named links |
| LineageTag (LT) | A lineage DAG node linking DUTs into a traceable process. | `cid` over content plus named links; the graph is identified by the root LT CID |
| Artifact | A hash record of uploaded bytes; AITS does not retain the bytes. | `cid` over the artifact node; `sha256` over the raw bytes |

Two consequences of content-addressing apply from the first call:

- Idempotency — recording the same `AgentConfig`, or uploading the same artifact
  bytes, twice returns the existing row instead of creating a duplicate.
- Verifiability — `GET /verify/{cid}/` recomputes a node's hash and recursively
  checks its links, while `GET /audit/` walks the whole ledger and commit log.

The [Your first ledger](docs/tutorials/your-first-ledger.md) tutorial walks
through recording, lineage, and tamper detection in a disposable database. See
[Architecture](docs/explanation/architecture.md) for the system design.

## Quickstart

Requirements: Python 3.13+, [uv](https://docs.astral.sh/uv/), and Docker (for the
development Postgres container). Use a fresh database for 0.3.0; see the
[upgrade guide](docs/howto/migrate-to-root-lt-lineage.md) for existing deployments.
The defaults require ports 5432 and 8000. If changing `POSTGRES_PORT` in `.env`,
also change the port in `DATABASE_URL`.

```bash
make deps                     # install dependencies from uv.lock
cp .env.example .env          # adjust DATABASE_URL if needed
make dev                      # boot Postgres + start FastAPI (auto-reload)
```

The `.env.example` template sets `AUTH_DISABLED=true`, so the local server
accepts unauthenticated requests. See [Authentication](#authentication) before
running AITS anywhere else.

The service runs at <http://127.0.0.1:8000>:

- Interactive API docs: <http://127.0.0.1:8000/docs>
- OpenAPI schema: <http://127.0.0.1:8000/openapi.json>
- Liveness check: <http://127.0.0.1:8000/health> (the process is up)
- Readiness check: <http://127.0.0.1:8000/ready> (the database is reachable;
  503 if not)

## Docker image

Build the server image from the project root:

```bash
docker build -t aits:local .
```

The image contains only the FastAPI service. It does not include Postgres or bake
in database credentials. Put the runtime database URL in an env file:

```bash
cp .env.example .env
# Edit DATABASE_URL to point at the database this container should use.
# Set AUTH_SERVICE_KEYS and remove AUTH_DISABLED (see Authentication).
```

Then pass that file to Docker when starting the server:

```bash
docker run --rm --env-file .env -p 8000:8000 aits:local
```

If you are connecting to the Postgres container from this repo's
`docker-compose.yml`, put the AITS container on the same Docker network and set
`DATABASE_URL` to the Compose service name `postgres`. From inside the AITS container,
`127.0.0.1` means the AITS container itself, not the Postgres container.

```env
DATABASE_URL=postgresql+psycopg://bastion:bastion@postgres:5432/bastion
```

```bash
docker run --rm \
  --network <compose-project>_default \
  --env-file .env \
  -p 8000:8000 \
  aits:local
```

Replace `<compose-project>` with the project name shown by `docker compose ls`.
Compose normally uses the directory name; `COMPOSE_PROJECT_NAME` or `-p`
overrides it. The internal PostgreSQL port remains 5432.

Downstream Docker Compose projects should use the same pattern with `env_file`:

```yaml
services:
  aits:
    image: aits:local
    env_file:
      - .env
    ports:
      - "8000:8000"
```

Compose also reads `.env` for `${VAR}` substitution in compose files; `env_file`
is what passes those variables into the AITS container.

## Authentication

AITS authenticates the services that call it; user authentication belongs in
the calling service. Every resource endpoint requires
`Authorization: Bearer <secret>`, with a secret from `AUTH_SERVICE_KEYS`:

```env
AUTH_SERVICE_KEYS=gateway:<long-random-secret>,recorder:<another-secret>
```

Each entry is `name:secret`. The name identifies the calling service. The
server refuses to start without keys unless `AUTH_DISABLED=true`, which is for
local development only. `/health`, `/ready`, `/docs`, `/redoc`, and `/openapi.json` stay open. The
generated SDK sends the key with `Configuration(access_token=...)`.

Every commit-log entry records a `principal`: the identity AITS authenticated
for that request, which is the key's name (`anonymous` in open mode). An
inference gateway is one such identity; any other holder of a key is another.
In authenticated mode,
writes are attributed to the authenticated service. Open mode accepts
unauthenticated requests and attributes writes to `anonymous`.
`GET /commits/` returns the `principal`, and it is
part of the entry hash.

See the [configuration reference](docs/reference/configuration.md) for defaults,
key syntax, and request limits.

## Security and support

This repository is a reference implementation of the Bastion-One model. It keeps
the API surface intentionally small. It authenticates calling services (see
[Authentication](#authentication)) and leaves user authentication,
authorization, network placement, and production deployment controls to the
implementer.

For commercial support or the more comprehensive enterprise product, contact
Bastion-One.

## Using the API

AITS ships a generated Python SDK (`aits-client`). Build it with
`make regen`. Save the example as `example.py` and run it with
`uv run --frozen --with ./clients/python python example.py`. It assumes the
local open-mode server from Quickstart. The core flow looks like this:

```python
from aits_client import ApiClient, Configuration, AgentsApi, AgentCreate, ConfigCreate

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
them into a lineage graph. The full walkthrough — DUTs, artifacts, lineage,
and verification — is in [Your first ledger](docs/tutorials/your-first-ledger.md).
See [Migrate to root-LT lineage](docs/howto/migrate-to-root-lt-lineage.md) if you
are updating from session-UUID APIs.

## Generated client

The checked-in `clients/build/` files are the source of truth for generating the
Python SDK; `clients/python/` is ephemeral and gitignored. Use:

```bash
make regen         # refresh openapi.json and regenerate clients/python/
make build-client  # build the generated SDK sdist + wheel
```

Run client builds through `make build-client`, not a top-level `uv build`.

`tools/aits_view.py` renders the ledger as a static HTML page and is built on
the generated client, so it needs the SDK regenerated and installed first.
Run it via:

```bash
make view                                    # all lineage graphs -> aits-view.html, opened
make view AITS_VIEW_ARGS='<root_cid>'          # replace with a graph's root CID
```

`make view` depends on `regen`, so the client is always freshly regenerated
from the local in-process app's schema before the script runs. It does not
fetch the running server's schema. Direct invocation without an installed SDK
fails with a message pointing back to this target. For a remote server, use
matching client code and pass `--url`; authentication uses `AITS_API_KEY` or
`--api-key`. Add `--no-open` for a headless run. The viewer lists at most 500
graphs and reads at most 100,000 commit entries.

## Development

Run `make help` for the full target list. Common tasks:

```bash
make deps          # install dependencies from uv.lock
make test          # unit tests (fast, sqlite)
make check         # formatting + pylint
make quality       # coverage, complexity, and mutation thresholds (Docker; PostgreSQL URL below)
make snyk          # dependency + SAST scans (requires mise-managed Snyk and authentication)
```

Set `AITS_TEST_DATABASE_URL` to a dedicated PostgreSQL test database before
running `make quality`, for example the disposable tutorial database. If
PostgreSQL is unreachable, its tests skip and the coverage gate fails. Run the
[tutorial](docs/tutorials/your-first-ledger.md) setup through database startup,
then set:

```bash
export AITS_TEST_DATABASE_URL=postgresql+psycopg://bastion:bastion@127.0.0.1:55432/bastion
```

The PostgreSQL tests create and drop isolated schemas; use a disposable test
database with permission to create schemas.

CI (`.github/workflows/ci.yml`) checks formatting, lint, and `make quality`,
and fails if `make openapi` changes `openapi.json`.

Quality checks require statement and branch coverage above 90%, every application
function's cyclomatic complexity below 20, and assertion kills for more than 95%
of all generated mutants. Coverage includes the regenerated SDK and PostgreSQL
tests; skipped tests fail the gate. Mutation testing uses a fixed
scope: `app/ledger.py`, `app/nodes.py`, `app/routers/common.py`, and
`app/routers/lineage.py` helpers. Timeouts and surviving equivalent mutations stay
in the denominator. All 17 hand-written mutation probes must also fail the
full test suite, including the decorated route handlers that mutmut skips.
Measurements and probe logs are written to `quality-results/` and uploaded by CI.

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

Start with the [documentation index](docs/README.md), organized by reader need:

- [Tutorial: Your first ledger](docs/tutorials/your-first-ledger.md).
- [How-to: Migrate to root-LT lineage](docs/howto/migrate-to-root-lt-lineage.md).
- [How-to: Record an inference and lineage node](docs/howto/record-inline-dut.md).
- [Reference: Lineage API](docs/reference/lineage.md) and [OpenAPI](openapi.json).
- [Reference: Configuration](docs/reference/configuration.md),
  [records](docs/reference/records.md), [verification](docs/reference/verification.md),
  and [ledger format](docs/reference/ledger-format.md).
- [Explanation: Architecture](docs/explanation/architecture.md).

See the [changelog](CHANGELOG.md) for versioned release notes.

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
docs/                 tutorials, how-to guides, reference, and explanation
tests/                unit suite (sqlite) + opt-in integration suite (SDK + server)
.claude/              Claude Code integration proof of concept
```

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).
