# Record an inference and lineage node atomically

Use this guide when you already have a running AITS service and the generated
SDK. See the [README](../../README.md#quickstart) for setup. Save the example
as `fastpath.py` and run it from the project root:

```bash
uv run --frozen --with ./clients/python python fastpath.py
```

The client below assumes local open mode. For an authenticated server, set
`Configuration(access_token=...)` as described in the
[authentication reference](../reference/configuration.md#authentication).

Register the agent once, retain a root CID, and record each round as an
inline-DUT node. This example records a sample result; it does not call an LLM.

```python
"""fastpath.py — register an agent, create a root, write inline-DUT nodes."""

from aits_client import (
    ApiClient,
    Configuration,
    AgentsApi,
    LineageApi,
    AuditApi,
    AgentCreate,
    ConfigCreate,
    LineageRootCreate,
    LineageNodeCreate,
    InlineDUTCreate,
)

client = ApiClient(Configuration(host="http://127.0.0.1:8000"))
agents, lineage, audit = AgentsApi(client), LineageApi(client), AuditApi(client)

agent = agents.register(
    AgentCreate(
        name="invoice-extractor",
        config=ConfigCreate(
            system_prompt="Extract line items from invoices as JSON.",
            llm_config={"model": "gpt-x", "temperature": 0.0},
        ),
    )
)
print("agent ", agent.uuid, "active config", agent.active_config_cid[:12])

root = lineage.create_root(
    LineageRootCreate(
        actor_id=agent.name, step_id="extract", transformation="root"
    )
)
round1 = lineage.create_lineage_node(
    root.cid,
    LineageNodeCreate(
        actor_id=agent.name,
        step_id="extract-INV-1007",
        transformation="invoice-extraction",
        prev=[root.cid],
        dut=InlineDUTCreate(
            agent_uuid=agent.uuid,
            span_id="extract-INV-1007",
            input_context="INV-1007\nWidget x4 @ 2.50\nGadget x1 @ 9.99\n",
            agent_output='[{"item": "Widget", "qty": 4}, {"item": "Gadget", "qty": 1}]',
        ),
    ),
)
print("dut   ", round1.dut.cid[:12], "| node", round1.node.cid[:12])
print("root  ", round1.root[:12])
print("verify", audit.verify(round1.node.cid).valid)
```

`create_lineage_node` writes the DUT span and its lineage node in one
transaction. An omitted inline `config_cid` uses the agent's **active**
config. Pass `dut_cid` to attach a DUT you already stored with `POST /duts/`,
or omit both DUT fields for a structural node. `prev` is required and
nonempty; the server does not pick the frontier for you.

When one agent consumes another's results, put the exact consumed LT CIDs in
`derived_from`. Traceback from the combined answer still reaches every
contributor's DUT. To fork within one graph, create two nodes with the same
`prev` parent.
To join their work, create a node with both branch CIDs in `prev`.
Use separate roots for independent graphs.

To record and activate a configuration in one call, use
`PUT /agents/{uuid}/config/` (`configs.set_config`). It selects the current
activation head without the compare-and-set handshake.

The final `verify` output should be `True`. See the
[lineage reference](../reference/lineage.md) for defaults and error responses.
