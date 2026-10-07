# Configuration and authentication reference

Settings load from environment variables, `.env`, and `.env.local` in the
working directory. Environment variables take precedence; `.env.local`
overrides `.env`. Names are case-insensitive. The Docker image receives
variables through `--env-file` or Compose `env_file`.

## Runtime settings

| Variable | Default | Behavior |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://bastion:bastion@127.0.0.1:5432/bastion` | SQLAlchemy connection URL |
| `AUTH_SERVICE_KEYS` | Empty | Comma-separated `name:secret` pairs |
| `AUTH_DISABLED` | `false` | Enables unauthenticated local mode when true |
| `COMMIT_LOCK_TIMEOUT_MS` | `5000` | Positive PostgreSQL commit-lock wait limit; timeout returns 503 and `Retry-After: 1` |
| `OCCURRED_AT_MAX_SKEW_SECONDS` | `300` | Nonnegative permitted future event-time skew; exceeding it returns 400; past times are accepted |
| `MAX_REQUEST_BYTES` | `67108864` | Positive body limit, including chunked requests and multipart uploads; exceeding it returns 413 |

`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, and `POSTGRES_PORT` configure
the development Compose container, not the application's connection. Keep
`DATABASE_URL` consistent with them. Inside the Compose network use service
name `postgres` and port 5432; host clients use the published port.

Startup creates missing tables; it does not migrate existing tables. AITS 0.3.0
requires a fresh database when upgrading from older formats. See the
[upgrade guide](../howto/migrate-to-root-lt-lineage.md).

## Authentication

In authenticated mode every resource route requires
`Authorization: Bearer <secret>`. Missing or incorrect credentials return 401
with `WWW-Authenticate: Bearer`. `/health`, `/ready`, `/docs`, `/redoc`, and
`/openapi.json` remain unauthenticated.

Service names contain 1–64 letters, digits, periods, underscores, or hyphens.
The secret is the nonempty remainder after the first colon. Duplicate names,
duplicate secrets, and malformed entries fail settings validation. The server
refuses to start without configured keys unless `AUTH_DISABLED=true`.

Open mode bypasses credential checking even if keys are configured. Writes
record `anonymous`; authenticated writes record the matched key's service
name as `principal`. A principal identifies the service credential presented,
not the end user or proof that an inference actually occurred.

The generated SDK sends a Bearer token through
`Configuration(access_token=...)`. The viewer and Claude ledger hook read
`AITS_API_KEY`; the viewer also accepts `--api-key`.

## Request constraints

| Input | Limit or rule |
| --- | --- |
| `system_prompt`, `input_context`, `agent_output` | 1,000,000 characters |
| Names, labels, identifiers | 2,048 characters |
| `tools`, `business_object_keys`, `artifact_cids`, `prev`, `derived_from` | 1,000 items |
| CID and artifact digest inputs | Exactly 64 hexadecimal characters; either case; no whitespace |
| Supplied event times | Must include a timezone; missing/null uses the route's default |
| Canonical JSON content | Finite numbers, valid Unicode, and integers within ±(2^53 − 1); encode larger integers as strings |

Field/list limit violations return 422. Validation errors omit the echoed
`input`. `llm_config`, `metadata`, and individual tool contents are bounded by
the total body limit and [canonical encoding rules](ledger-format.md), rather
than separate field-size limits. Lineage request models reject unknown fields;
this rule does not apply to every request model.

`GET /agents/` uses `limit` (default 100, maximum 1000) and `offset` (default 0).
Lineage roots use a `limit` default of 100 without an offset parameter.
Commit-log reads use `limit` (default 100, bounded to 1 through 1000) and page
with `after_seq`; see [verification](verification.md). A truncated commit-log
response is not a complete history.
