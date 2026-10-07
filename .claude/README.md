# Claude Code ledger hooks

The hooks in `settings.json` use the root-LT API (AITS
0.3.0). Set `AITS_URL` to the ledger URL; the default is
`http://127.0.0.1:8000`. `AITS_HOOK_TIMEOUT` sets the timeout in seconds for
each HTTP request (default: 3). Hook failures do not interrupt Claude Code.

Set `AITS_API_KEY` to a service key when the ledger requires one (see
Authentication in the top-level README). Without it, an authenticated ledger
rejects every call and nothing is recorded, silently. Run
`python3 .claude/hooks/aits_ledger.py status` to confirm the key is set.
`tools/aits_view.py` reads the same variable, or takes `--api-key`.

`SessionStart` registers or refreshes the project agent and creates a lineage
root. Claude session IDs are local lookup keys, not AITS identifiers. Each
session's root and latest returned LT CID are stored in ignored
`.claude/aits/lineage-*.json` files, scoped to the ledger URL. Resume and
compact events retain the root. A new session ID gets a new root.

Tool calls and assistant responses create nodes with an explicit `prev`
parent and a nested inline `dut`. Subagent tool calls use a separate root;
the handoff records the cached subagent head in `derived_from`. If no
subagent node is available, the handoff has no cross-root link.

A file lock serializes hook processes on macOS/Linux to protect cached
agent and lineage state. Parents reflect hook processing order. Async hooks
can arrive out of event order; a handoff links only the subagent work already
recorded when it runs. The session-end audit also covers only writes already
committed. Recording remains best-effort, bounded by the hook timeouts in
`settings.json`.

A missing cached root or agent is recreated when the ledger has been reset.
Old session UUIDs are never converted into CIDs. Follow
[the migration guide](../docs/howto/migrate-to-root-lt-lineage.md) when
upgrading the service; these hooks do not migrate historical data.

Toggle recording from the project directory:

```sh
python3 .claude/hooks/aits_ledger.py off
python3 .claude/hooks/aits_ledger.py on
python3 .claude/hooks/aits_ledger.py status
```

Run the hook contract tests from the repository root:

```sh
uv run --frozen pytest tests/hooks
```
