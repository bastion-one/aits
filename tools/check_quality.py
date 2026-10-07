"""Enforce strict coverage, function complexity, and scoped mutation limits."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import tomllib

from defusedxml import ElementTree as ET
from radon.complexity import cc_visit
from radon.visitors import Class


def check_coverage():
    report = json.loads(Path("quality-results/coverage.json").read_text(encoding="utf-8"))
    totals = report["totals"]
    statements = totals["percent_statements_covered"]
    branches = totals["percent_branches_covered"]
    print(f"Statement coverage: {statements:.2f}%; branch coverage: {branches:.2f}%")
    tests = ET.parse("quality-results/tests.xml").getroot()
    skipped = sum(int(suite.get("skipped", 0)) for suite in tests.iter("testsuite"))
    failures = sum(
        int(suite.get("failures", 0)) + int(suite.get("errors", 0))
        for suite in tests.iter("testsuite")
    )
    count = sum(int(suite.get("tests", 0)) for suite in tests.iter("testsuite"))
    print(f"Tests: {count}; skipped: {skipped}; failures/errors: {failures}")
    return statements > 90 and branches > 90 and count > 0 and skipped == failures == 0


def functions(blocks):
    for block in blocks:
        if isinstance(block, Class):
            yield from functions(block.methods)
            yield from functions(block.inner_classes)
        else:
            yield block
            yield from functions(block.closures)


def check_complexity():
    measured = [
        (block.complexity, str(path), block.name, block.lineno)
        for path in Path("app").rglob("*.py")
        for block in functions(cc_visit(path.read_text(encoding="utf-8")))
    ]
    highest = max(measured)
    print(f"Maximum function complexity: {highest[0]} ({highest[1]}:{highest[3]} {highest[2]})")
    for complexity, path, name, line in measured:
        if complexity >= 20:
            print(f"FAIL: {path}:{line} {name}: {complexity}")
    return highest[0] < 20


def check_mutation():
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["tool"]["mutmut"]
    counts = Counter()
    statuses = {
        1: "killed",
        0: "survived",
        5: "no_tests",
        33: "no_tests",
        36: "timeout",
        -24: "timeout",
        24: "timeout",
        152: "timeout",
        255: "timeout",
        None: "unchecked",
    }
    by_file = {}
    for source in config["only_mutate"]:
        path = Path("mutants") / (source + ".meta")
        values = json.loads(path.read_text(encoding="utf-8"))["exit_code_by_key"].values()
        file_counts = Counter(statuses.get(value, "error") for value in values)
        if not file_counts:
            raise ValueError(f"No mutants generated for {source}")
        by_file[source] = dict(file_counts)
        counts.update(file_counts)
    total = sum(counts.values())
    score = 100 * counts["killed"] / total
    print(f"Mutation assertion kills / all generated: {counts['killed']}/{total} ({score:.2f}%)")
    print(dict(counts))
    Path("quality-results").mkdir(exist_ok=True)
    Path("quality-results/mutation.json").write_text(
        json.dumps(
            {"counts": dict(counts), "by_file": by_file, "generated": total, "kill_percent": score},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # Timeouts stay in the denominator and never count as assertion kills.
    return score > 95 and counts["unchecked"] == counts["error"] == counts["no_tests"] == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("measurement", choices=["coverage", "complexity", "mutation"])
    args = parser.parse_args()
    checks = {
        "coverage": check_coverage,
        "complexity": check_complexity,
        "mutation": check_mutation,
    }
    if not checks[args.measurement]():
        sys.exit("Quality threshold failed")


if __name__ == "__main__":
    main()
