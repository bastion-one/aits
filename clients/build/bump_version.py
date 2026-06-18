"""Single source of truth for the generated client's version.

The wheel's version comes from `[project].version` in clients/build/pyproject.toml
(overlaid onto the ephemeral generated tree). A second `packageVersion` in
openapi-generator-config.json feeds the generated client's embedded user-agent.
This script bumps both in lockstep so they never drift.

Usage:
    python bump_version.py {major|minor|patch}   # bump and rewrite both files
    python bump_version.py --print               # print current version, no changes
"""

import re
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYPROJECT = HERE / "pyproject.toml"
GENERATOR_CONFIG = HERE / "openapi-generator-config.json"


def current_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["version"]


def next_version(version: str, part: str) -> str:
    # Take only the release segment (drop any pre/post/dev suffix) and bump it.
    release = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    if not release:
        raise SystemExit(f"cannot parse semver release from version {version!r}")
    major, minor, patch = (int(g) for g in release.groups())
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    elif part == "patch":
        patch += 1
    else:
        raise SystemExit(f"unknown bump part {part!r} (expected major|minor|patch)")
    return f"{major}.{minor}.{patch}"


def _replace_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(pattern, replacement, text, count=1)
    if count != 1:
        raise SystemExit(f"expected exactly one version match in {path}, found {count}")
    path.write_text(new_text, encoding="utf-8")


def write_version(version: str) -> None:
    # Targeted line replacement -- preserve formatting/comments, don't reserialize.
    _replace_once(
        PYPROJECT,
        r'(?m)^version\s*=\s*"[^"]*"',
        f'version = "{version}"',
    )
    _replace_once(
        GENERATOR_CONFIG,
        r'"packageVersion"\s*:\s*"[^"]*"',
        f'"packageVersion": "{version}"',
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    arg = argv[1]
    if arg == "--print":
        print(current_version())
        return 0

    version = next_version(current_version(), arg)
    write_version(version)
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
