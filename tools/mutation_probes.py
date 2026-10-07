"""Replay the hand-written decorated-route and known-survivor mutation probes in isolation."""

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from defusedxml import ElementTree as ET

# Each probe changes one function independently. Do not merge this hand-selected
# result into mutmut's automated score.
PROBES = [
    (
        "drop_inline_business_keys",
        "app/routers/lineage.py",
        "_inline_dut_create",
        "        business_object_keys=inline.business_object_keys,\n",
        "",
    ),
    (
        "erase_activation_content",
        "app/nodes.py",
        "_activation_node",
        'content = {"v": row.v, "type": "ConfigActivation", "agent_uuid": row.agent_uuid}',
        "content = None",
    ),
    (
        "change_artifact_type",
        "app/nodes.py",
        "_artifact_node",
        '"type": "Artifact"',
        '"type": "XXArtifactXX"',
    ),
    (
        "rename_agent_type_key",
        "app/nodes.py",
        "_agent_node",
        '"type": "Agent"',
        '"XXtypeXX": "Agent"',
    ),
    (
        "drop_commit",
        "app/routers/lineage.py",
        "create_lineage_node",
        "        session.commit()",
        "        pass  # mutation: no commit",
    ),
    (
        "drop_rollback",
        "app/routers/lineage.py",
        "create_lineage_node",
        "            session.rollback()",
        "            pass  # mutation: no rollback",
    ),
    (
        "commit_dut_early",
        "app/routers/lineage.py",
        "create_lineage_node",
        "            dut_cid = attached.cid",
        "            dut_cid = attached.cid\n            session.commit()",
    ),
    (
        "accept_wrong_parent_root",
        "app/routers/lineage.py",
        "create_lineage_node",
        "if parent_root != root:",
        "if False:",
    ),
    (
        "accept_same_root_derived",
        "app/routers/lineage.py",
        "create_lineage_node",
        "if source_root == root:",
        "if False:",
    ),
    (
        "drop_prev_links",
        "app/routers/lineage.py",
        "create_lineage_node",
        "prev_cids=hex_cid_list(prev),",
        "prev_cids=[], ",
    ),
    (
        "drop_derived_links",
        "app/routers/lineage.py",
        "create_lineage_node",
        "derived_from_cids=hex_cid_list(derived),",
        "derived_from_cids=[], ",
    ),
    (
        "drop_dut_link",
        "app/routers/lineage.py",
        "create_lineage_node",
        "                dut_cid=dut_cid,",
        "                dut_cid=None,",
    ),
    (
        "wrong_location",
        "app/routers/lineage.py",
        "create_lineage_node",
        'f"/lineage/nodes/{stored.cid.hex()}/"',
        'f"/lineage/nodes/{root.hex()}/"',
    ),
    (
        "wrong_response_root",
        "app/routers/lineage.py",
        "create_lineage_node",
        "    root_hex = root.hex()",
        "    root_hex = stored.cid.hex()",
    ),
    (
        "accept_both_dut_modes",
        "app/routers/lineage.py",
        "create_lineage_node",
        "if body.dut_cid is not None and body.dut is not None:",
        "if False:",
    ),
    (
        "ignore_inline_event_time",
        "app/routers/lineage.py",
        "create_lineage_node",
        'else ensure_aware(body.dut.occurred_at, field="dut.occurred_at")',
        "else occurred_at",
    ),
    (
        "omit_existing_dut_response",
        "app/routers/lineage.py",
        "create_lineage_node",
        "attached: DataUniqueTag | None = existing_dut",
        "attached: DataUniqueTag | None = None",
    ),
]


def run_suite(work, output):
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-m", "", "--junitxml=tests.xml"],
        cwd=work,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=90,
        check=False,
    )
    output.write_text(run.stdout, encoding="utf-8")
    return run


def main():
    root = Path.cwd()
    output = root / "quality-results/probes"
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="aits-route-probes-") as directory:
        work = Path(directory)
        for name in ("app", "tests", ".claude/hooks"):
            shutil.copytree(root / name, work / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(root / "pyproject.toml", work / "pyproject.toml")
        baseline = run_suite(work, output / "baseline.txt")
        suites = list(ET.parse(work / "tests.xml").getroot().iter("testsuite"))
        if baseline.returncode != 0 or any(int(s.get("skipped", 0)) for s in suites):
            sys.exit(
                "Probe baseline failed or skipped tests; see quality-results/probes/baseline.txt"
            )
        for name, filename, function, old, new in PROBES:
            path = work / filename
            original = path.read_text(encoding="utf-8")
            node = next(
                n
                for n in ast.parse(original).body
                if isinstance(n, ast.FunctionDef) and n.name == function
            )
            lines = original.splitlines(keepends=True)
            start, end = node.lineno - 1, node.end_lineno
            section = "".join(lines[start:end])
            if section.count(old) != 1:
                sys.exit(f"Probe {name} no longer matches exactly one source location")
            path.write_text(
                "".join(lines[:start]) + section.replace(old, new) + "".join(lines[end:]),
                encoding="utf-8",
            )
            try:
                run = run_suite(work, output / f"{name}.txt")
                suites = ET.parse(work / "tests.xml").getroot().iter("testsuite")
                skipped = sum(int(suite.get("skipped", 0)) for suite in suites)
                result = (
                    "killed"
                    if run.returncode == 1
                    else "survived" if run.returncode == 0 else "error"
                )
                if skipped:
                    result = "error"
                results.append(
                    {
                        "name": name,
                        "file": filename,
                        "function": function,
                        "old": old,
                        "new": new,
                        "result": result,
                        "exit_code": run.returncode,
                        "summary": run.stdout.strip().splitlines()[-1],
                    }
                )
            except subprocess.TimeoutExpired as exc:
                (output / f"{name}.txt").write_bytes(exc.stdout or b"")
                results.append({"name": name, "result": "timeout"})
            finally:
                path.write_text(original, encoding="utf-8")
            (output / "results.json").write_text(
                json.dumps(results, indent=2) + "\n", encoding="utf-8"
            )
            print(f"{name}: {results[-1]['result']}", flush=True)
    if any(result["result"] != "killed" for result in results):
        sys.exit("Supplemental probes must all be killed")
    print(f"All {len(results)} supplemental probes killed")


if __name__ == "__main__":
    main()
