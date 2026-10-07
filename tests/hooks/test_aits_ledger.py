"""Exercise Claude hooks against the current API contract."""

import importlib.util
import io
import json
from pathlib import Path
import urllib.error

import pytest


@pytest.fixture
def hook(tmp_path, monkeypatch, client):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    path = Path(__file__).resolve().parents[2] / ".claude/hooks/aits_ledger.py"
    spec = importlib.util.spec_from_file_location("aits_ledger_hook", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def api(method, path, body=None):
        response = client.request(method, path, json=body)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(path, response.status_code, response.text, {}, None)
        return response.json()

    monkeypatch.setattr(module, "api", api)
    return module


def test_session_resume_tools_and_response(hook, tmp_path):
    """Retain a resumed session's root and chain its work; give a new session a new root."""
    hook.on_session_start({"session_id": "session", "model": "test"})
    initial = hook.ensure_lineage("session")
    hook.on_session_start({"session_id": "session", "source": "resume", "model": "test"})
    assert hook.ensure_lineage("session") == initial
    for tool in ("Read", "Edit"):
        hook.on_post_tool_use({"session_id": "session", "tool_name": tool})
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps({"type": role, "message": {"content": text}})
            for role, text in (("user", "Fix it"), ("assistant", "Done"))
        )
    )
    hook.on_stop({"session_id": "session", "transcript_path": str(transcript)})
    state = hook.ensure_lineage("session")
    graph = hook.api("GET", f"/lineage/{state['root']}/")
    assert len(graph["nodes"]) == 4
    assert graph["frontier"] == [state["head"]]
    previous = initial["root"]
    for node in graph["nodes"][1:]:
        assert node["prev"] == [previous]
        assert node["dut"]
        previous = node["cid"]
    hook.on_session_end({"session_id": "session"})
    assert "valid=True" in hook.AUDIT_LOG.read_text()
    hook.on_session_start({"session_id": "new-session", "source": "clear"})
    assert hook.ensure_lineage("new-session")["root"] != state["root"]


@pytest.mark.parametrize("has_tools", [False, True])
def test_subagent_handoff(hook, has_tools):
    """Link a handoff to the recorded child head, or omit the link when no work exists."""
    payload = {"session_id": "parent", "agent_id": "child", "agent_type": "Explore"}
    if has_tools:
        hook.on_post_tool_use({**payload, "tool_name": "Read"})
    hook.on_subagent_stop({**payload, "last_assistant_message": "Result"})
    parent = hook.ensure_lineage("parent")
    node = hook.api("GET", f"/lineage/nodes/{parent['head']}/")
    if has_tools:
        child = hook.ensure_lineage(hook.sub_session("parent", "child"))
        assert node["derived_from"] == [child["head"]]
        assert child["root"] != parent["root"]
    else:
        assert node["derived_from"] == []


def test_stale_agent_and_lineage_cache(hook):
    """Recover from a ledger reset without reusing missing agent or lineage identities."""
    hook.STATE_DIR.mkdir(parents=True)
    hook.AGENT_FILE.write_text(json.dumps({"uuid": "00000000-0000-4000-8000-000000000000"}))
    hook.lineage_file("session").write_text(json.dumps({"root": "0" * 64, "head": "0" * 64}))
    hook.on_post_tool_use({"session_id": "session", "tool_name": "Read"})
    state = hook.ensure_lineage("session")
    assert state["root"] != "0" * 64
    assert state["head"] != state["root"]
    assert hook.api("GET", "/audit/")["valid"]


def test_disabled_and_unavailable_ledger(hook, monkeypatch):
    """Let Claude continue when recording is disabled or the ledger cannot be reached."""
    monkeypatch.setattr(hook.sys, "argv", ["aits_ledger.py"])
    payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "session"})
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(payload))
    hook.toggle("off")
    assert hook.main() == 0
    assert not hook.AGENT_FILE.exists()
    hook.toggle("on")

    def unavailable(*args, **kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(hook, "api", unavailable)
    assert hook.main() == 0
    assert not hook.AGENT_FILE.exists()


def _load_hook(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    path = Path(__file__).resolve().parents[2] / ".claude/hooks/aits_ledger.py"
    spec = importlib.util.spec_from_file_location("aits_ledger_hook_raw", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _captured_request(module, monkeypatch):
    sent = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout):
        sent["headers"] = dict(req.header_items())
        return Response(b"{}")

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    module.api("GET", "/agents/")
    return sent["headers"]


def test_api_sends_the_service_key_as_a_bearer_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AITS_API_KEY", "s3cret")
    module = _load_hook(monkeypatch, tmp_path)
    assert _captured_request(module, monkeypatch)["Authorization"] == "Bearer s3cret"


def test_api_sends_no_authorization_without_a_key(monkeypatch, tmp_path):
    monkeypatch.delenv("AITS_API_KEY", raising=False)
    module = _load_hook(monkeypatch, tmp_path)
    assert "Authorization" not in _captured_request(module, monkeypatch)


def test_status_reports_whether_a_key_is_set(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("AITS_API_KEY", raising=False)
    _load_hook(monkeypatch, tmp_path).toggle("status")
    assert "key: NOT SET" in capsys.readouterr().out
    monkeypatch.setenv("AITS_API_KEY", "s3cret")
    _load_hook(monkeypatch, tmp_path).toggle("status")
    out = capsys.readouterr().out
    assert "key: set" in out
    assert "s3cret" not in out
