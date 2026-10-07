# Ledger format reference

Format 1 is the ledger format introduced in AITS 0.3.0. The format record is
`{"format": 1, "hash": "sha2-256", "canonical": "rfc8785"}`. It is stored
outside the hashed nodes.

## Canonical encoding

The canonical encoding is RFC 8785 (JSON Canonicalization Scheme) over a small
projection of AITS values onto JSON: bytes become base64 strings, timestamps
become UTC ISO-8601 strings ending in `Z`, UUIDs become lowercase strings, and
sets become arrays sorted by each member's canonical bytes. An independent
verifier can reproduce any CID with an RFC 8785 library plus that projection
(see `app/canonical_json.py`). RFC 8785 limits integers to +/-(2^53 - 1);
content with larger integers is rejected with 400, so send such values, like
64-bit seeds, as strings.

Bytes use standard padded base64. Sets are sorted by each member's canonical
JSON bytes; ordinary arrays retain their order. Empty or null link roles are
omitted. Canonical encoding allows at most 64 container levels, including
the node wrapper and content object; metadata can nest 62 levels.

## Node shapes

To reproduce a CID outside AITS, hash the canonical encoding of
`{"content": ..., "links": ...}` built from the API's view of the node. In
format 1, `v` is 1 for every node and the shapes are:

| Node | `content` keys (`type` value) | `links` roles |
|------|-------------------------------|---------------|
| `Agent` | `v`, `type` (`Agent`), `uuid`, `created_at` | none |
| `AgentConfig` | `v`, `type` (`AgentConfig`), `system_prompt`, `llm_config`, `tools`, `metadata` | none |
| `ConfigActivation` | `v`, `type` (`ConfigActivation`), `agent_uuid` | `agent`, `config`, `prev` |
| `DataUniqueTag` | `v`, `type` (`DUT`), `span_id`, `business_object_keys`, `sequence`, `input_context`, `agent_output`, `occurred_at` | `agent`, `config`, `artifacts` (set) |
| `LineageTag` | `v`, `type` (`LT`), `actor_id`, `step_id`, `transformation`, `occurred_at` | `prev` (set), `derived_from` (set), `dut` |
| `Artifact` | `v`, `type` (`Artifact`), `sha256` (lowercase hex) | none |

A link target is the CID's 32 raw bytes, so it encodes as base64, not hex. A
role with no target, or an empty set, is left out. An activation's `agent`
link is the agent's CID, from `GET /agents/{agent_uuid}/`. Timestamps are encoded with
microsecond precision, as in `2026-10-02T14:20:34.226218Z`, and the fraction is
dropped when it is zero (`2026-10-02T14:20:34Z`).

## Reproduce a configuration CID

This example uses only the public API and RFC 8785, without importing AITS
hashing code. Save it as `verify-config.py` and supply a configuration CID
from the tutorial. It assumes the local open-mode server:

```python
import hashlib
import json
import sys
import urllib.request

import rfc8785

cid = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:8000/configs/{cid}/") as response:
    config = json.load(response)
content = {"v": 1, "type": "AgentConfig"}
for key in ("system_prompt", "llm_config", "tools", "metadata"):
    content[key] = config[key]
encoded = rfc8785.dumps({"content": content, "links": {}})
recomputed = hashlib.sha256(encoded).hexdigest()
assert recomputed == config["cid"]
print(recomputed)
```

```bash
uv run --frozen python verify-config.py <config-cid>
```

The printed hash must equal the supplied lowercase CID. Other node types
require the byte, timestamp, and set projections listed above.

## Commit-entry hashes

Each entry hashes the canonical JSON object containing `cid`, `recorded_at`,
`prev`, and `principal`. The response field `prev_hash` maps to the hashed key
`prev`. CID and predecessor hashes use the byte projection above. The first
entry uses 32 zero bytes for `prev_hash`.
`seq` and `entry_hash` are not included in this object. See
[verification](verification.md) for what these hashes establish.
