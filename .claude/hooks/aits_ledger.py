#!/usr/bin/env python3
"""Claude Code -> AITS ledger hook (stdlib only).

Records this project's Claude Code activity into the local AITS ledger:

- SessionStart   register/refresh the agent + its config fingerprint
- PostToolUse    record each tool call as a DUT + lineage node
- Stop           record the assistant turn from the transcript
- SubagentStop   record the handoff merge (derived_from)
- SessionEnd     append a full-audit receipt to .claude/aits/audit.log

Toggle (no settings edit needed):

    python3 .claude/hooks/aits_ledger.py off | on | status

Set ``AITS_API_KEY`` to a service key when the ledger requires one; ``status``
reports whether it is set. Without it, an authenticated ledger rejects every
call.

Recording is best-effort by design: any failure (ledger down, bad payload,
missing key) exits 0 silently so the session is never disturbed. AITS detects; it does
not gate.
"""

import fcntl
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

AITS_URL = os.environ.get("AITS_URL", "http://127.0.0.1:8000")
API_KEY = os.environ.get("AITS_API_KEY", "")
HTTP_TIMEOUT = float(os.environ.get("AITS_HOOK_TIMEOUT", "3"))
TRUNCATE = 50_000

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
STATE_DIR = PROJECT / ".claude" / "aits"
OFF_FLAG = STATE_DIR / "off"
AGENT_FILE = STATE_DIR / "agent.json"
AUDIT_LOG = STATE_DIR / "audit.log"

# Namespace for deriving a deterministic sub-session uuid per (session, subagent).
SUBAGENT_NS = uuid.UUID("a175c0de-ad00-4000-8000-00c1aedec0de")


def api(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    req = urllib.request.Request(AITS_URL + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def clip(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) > TRUNCATE:
        return text[:TRUNCATE] + f"...[truncated {len(text) - TRUNCATE} chars]"
    return text


def config_body(model: str) -> dict:
    claude_md = PROJECT / "CLAUDE.md"
    return {
        "system_prompt": claude_md.read_text() if claude_md.exists() else "",
        "llm_config": {"model": model},
        "tools": [],
        "metadata": {"integration": "claude-code", "project": PROJECT.name},
    }


def ensure_agent(model: str = "unknown") -> str:
    """Return the ledger agent uuid for this project, registering or
    refreshing its config (drift -> a new activation) as needed."""
    if AGENT_FILE.exists():
        agent_uuid = json.loads(AGENT_FILE.read_text())["uuid"]
        try:
            api("PUT", f"/agents/{agent_uuid}/config/", config_body(model))
            return agent_uuid
        except urllib.error.HTTPError as exc:
            if exc.code != 404:  # 404: ledger was reset; fall through and re-register
                raise
    agent = api(
        "POST",
        "/agents/",
        {"name": f"claude-code:{PROJECT.name}", "config": config_body(model)},
    )
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    AGENT_FILE.write_text(json.dumps({"uuid": agent["uuid"]}))
    return agent["uuid"]


def cached_agent() -> "str | None":
    if AGENT_FILE.exists():
        return json.loads(AGENT_FILE.read_text())["uuid"]
    return None


def lineage_file(session_id: str) -> Path:
    # Claude session IDs are local lookup keys, never ledger identifiers.
    key = hashlib.sha256(f"{AITS_URL}/{session_id}".encode()).hexdigest()
    return STATE_DIR / f"lineage-{key}.json"


def ensure_lineage(session_id: str) -> dict:
    path = lineage_file(session_id)
    if path.exists():
        state = json.loads(path.read_text())
        try:
            api("GET", f"/lineage/nodes/{state['head']}/")
            return state
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    root = api(
        "POST",
        "/lineage/",
        {"actor_id": "claude-code", "step_id": session_id, "transformation": "root"},
    )
    state = {"root": root["cid"], "head": root["cid"]}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    return state


def record(session_id: str, body: dict) -> None:
    """Write an inline DUT with an explicit parent; retain the returned LT CID."""
    state = ensure_lineage(session_id)
    dut = {key: body.pop(key) for key in ("span_id", "input_context", "agent_output")}
    dut["agent_uuid"] = cached_agent() or ensure_agent()
    body.update(prev=[state["head"]], dut=dut)
    try:
        created = api("POST", f"/lineage/{state['root']}/nodes/", body)
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 409):
            raise
        # Refresh a missing agent/config without discarding provenance links.
        dut["agent_uuid"] = ensure_agent()
        created = api("POST", f"/lineage/{state['root']}/nodes/", body)
    state["head"] = created["node"]["cid"]
    lineage_file(session_id).write_text(json.dumps(state))


def sub_session(session_id: str, agent_id: str) -> str:
    return str(uuid.uuid5(SUBAGENT_NS, f"{session_id}/{agent_id}"))


def transcript_tail(path: str, role: str) -> str:
    """Last text of the given role ('assistant'/'user') from a transcript."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != role:
            continue
        message = entry.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text = "\n".join(
                block.get("text", "") for block in content if block.get("type") == "text"
            )
            if text.strip():
                return text
    return ""


def on_session_start(payload: dict) -> None:
    ensure_agent(payload.get("model", "unknown"))
    if payload.get("session_id"):
        ensure_lineage(payload["session_id"])


def on_post_tool_use(payload: dict) -> None:
    session = payload["session_id"]
    if payload.get("agent_id"):
        session = sub_session(session, payload["agent_id"])
    tool = payload.get("tool_name", "unknown")
    record(
        session,
        {
            "span_id": payload.get("tool_use_id") or uuid.uuid4().hex,
            "input_context": clip(payload.get("tool_input", {})),
            "agent_output": clip(payload.get("tool_response", payload.get("tool_output", ""))),
            "transformation": f"tool:{tool}",
            "actor_id": payload.get("agent_type") or "claude-code",
            "step_id": tool,
        },
    )


def on_stop(payload: dict) -> None:
    text = transcript_tail(payload.get("transcript_path", ""), "assistant")
    if not text.strip():
        return
    record(
        payload["session_id"],
        {
            "span_id": f"turn-{uuid.uuid5(SUBAGENT_NS, text[:2000])}",
            "input_context": clip(transcript_tail(payload.get("transcript_path", ""), "user")),
            "agent_output": clip(text),
            "transformation": "respond",
            "actor_id": "claude-code",
            "step_id": "turn",
        },
    )


def on_subagent_stop(payload: dict) -> None:
    agent_id = payload.get("agent_id")
    if not agent_id:
        return
    source_file = lineage_file(sub_session(payload["session_id"], agent_id))
    derived_from = []
    if source_file.exists():
        source = json.loads(source_file.read_text())
        try:
            api("GET", f"/lineage/nodes/{source['head']}/")
            derived_from = [source["head"]]
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    record(
        payload["session_id"],
        {
            "span_id": f"subagent-{agent_id}",
            "input_context": f"subagent {payload.get('agent_type', 'unknown')} ({agent_id})",
            "agent_output": clip(payload.get("last_assistant_message", "")),
            "transformation": f"subagent:{payload.get('agent_type', 'unknown')}",
            "actor_id": "claude-code",
            "step_id": "handoff",
            "derived_from": derived_from,
        },
    )


def on_session_end(payload: dict) -> None:
    report = api("GET", "/audit/")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a") as log:
        log.write(
            f"{datetime.now(timezone.utc).isoformat()} session={payload.get('session_id')} "
            f"valid={report['valid']} nodes={report['nodes_checked']} "
            f"entries={report['commit_entries']} failures={len(report['failures'])}\n"
        )


HANDLERS = {
    "SessionStart": on_session_start,
    "PostToolUse": on_post_tool_use,
    "Stop": on_stop,
    "SubagentStop": on_subagent_stop,
    "SessionEnd": on_session_end,
}


def toggle(command: str) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if command == "off":
        OFF_FLAG.touch()
        print("AITS ledger recording: OFF")
    elif command == "on":
        OFF_FLAG.unlink(missing_ok=True)
        print("AITS ledger recording: ON")
    elif command == "status":
        state = "OFF" if OFF_FLAG.exists() else "ON"
        agent = json.loads(AGENT_FILE.read_text())["uuid"] if AGENT_FILE.exists() else "(none)"
        key = "set" if API_KEY else "NOT SET"
        print(f"recording: {state} | ledger: {AITS_URL} | key: {key} | agent: {agent}")
    else:
        print(f"usage: {sys.argv[0]} on|off|status", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        return toggle(sys.argv[1])
    if OFF_FLAG.exists():
        return 0
    try:
        payload = json.load(sys.stdin)
        handler = HANDLERS.get(payload.get("hook_event_name", ""))
        if handler is not None:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            # Async hook processes share the agent cache and lineage heads.
            with (STATE_DIR / "ledger.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                handler(payload)
    except Exception:  # pylint: disable=broad-exception-caught
        pass  # never disturb the session; AITS detects, it does not gate
    return 0


if __name__ == "__main__":
    sys.exit(main())
