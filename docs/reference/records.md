# Configuration, activation, and artifact reference

See [OpenAPI](../../openapi.json) for full schemas and
[configuration](configuration.md) for authentication and shared limits.

## Configurations and activations

| HTTP operation | Generated `ConfigsApi` method | Result |
| --- | --- | --- |
| `POST /configs/` | `create(config_create)` | `ConfigRead`, 201 |
| `GET /configs/{cid_hex}/` | `get(cid_hex)` | `ConfigRead` |
| `POST /agents/{agent_uuid}/activations/` | `activate(agent_uuid, activation_create)` | `ActivationRead`, 201 |
| `PUT /agents/{agent_uuid}/config/` | `set_config(agent_uuid, config_create)` | `ActivationRead` |
| `GET /agents/{agent_uuid}/activations/` | `activation_history(agent_uuid)` | Activations from genesis to head |
| `GET /agents/{agent_uuid}/active-config/` | `active_config(agent_uuid)` | `ConfigRead`; 404 if no active configuration |

`ConfigCreate` requires `system_prompt`. `llm_config` and `metadata` default to
empty objects; `tools` defaults to an empty array. Configurations are immutable
behavioral values shared across agents. Identical submissions return the same
CID and add a commit-log observation.

`ActivationCreate` requires `config_cid`. `expected_head` is the current
activation CID, not the configuration CID. Missing/null means there should be
no activation yet. A stale head returns 409. The agent and config must exist
or the operation returns 404. Head selection and comparison occur under the
PostgreSQL commit lock.

Setting a configuration uses the server-selected activation head without the
compare-and-set guard. Activating the already-active configuration returns the
existing activation without adding another activation observation. The PUT
operation still records an observation of its submitted configuration.

`ActivationRead` contains `cid`, `agent_uuid`, `config_cid`, and `prev_cid`.
An activation declares intended configuration; a DUT records the caller's
reported configuration. AITS does not automatically compare them for drift.

## Artifacts

| HTTP operation | Generated `ArtifactsApi` method | Result |
| --- | --- | --- |
| `POST /artifacts/` | `upload(body)` | `ArtifactRead`, 201; raw `application/octet-stream` bytes |
| `POST /artifacts/upload/` | `upload_file(file, ...)` | `ArtifactRead`, 201; multipart file |
| `GET /artifacts/{cid_hex}/` | `get(cid_hex)` | `ArtifactRead` |
| `GET /artifacts/by-digest/{sha256_hex}/` | `get_by_digest(sha256_hex)` | `ArtifactRead` |
| `PATCH /artifacts/{cid_hex}/` | `set_locator(cid_hex, artifact_update)` | `ArtifactRead` |
| `POST /artifacts/{cid_hex}/aliases/` | `add_alias(cid_hex, alias_create)` | `AliasRead`, 201 |
| `GET /artifacts/resolve/` | `resolve(source, alias)` | `{cid, source, alias}` |

An artifact retains the SHA-256 of uploaded bytes, not the bytes themselves.
Its CID hashes the artifact node; `sha256` hashes the raw file. Identical bytes
deduplicate and add observations. Missing artifacts or aliases return 404.

`locator` is a mutable external-storage hint outside the CID. A patch requires
the `locator` field; null clears it. An alias maps a `(source, alias)` pair to
one artifact. Reusing that pair for the same artifact returns the existing
alias; mapping it to different bytes returns 409. Aliases are also outside the
CID and are not covered by node verification.

Multipart upload hashes in 1 MiB chunks. `source` defaults to `upload`; `alias`
defaults to the filename. Artifact recording and alias registration are
separate commits: an alias conflict can return 409 after the artifact and its
observation have been recorded.
