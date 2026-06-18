"""Shim OpenAPI 3.1 binary schemas for openapi-generator."""

import json
import sys
from pathlib import Path
from typing import Any


def _shim_binary_schemas(value: Any) -> Any:
    if isinstance(value, dict):
        shimmed = {key: _shim_binary_schemas(item) for key, item in value.items()}
        if (
            shimmed.get("type") == "string"
            and shimmed.get("contentMediaType") == "application/octet-stream"
        ):
            shimmed.pop("contentMediaType")
            shimmed["format"] = "binary"
        return shimmed
    if isinstance(value, list):
        return [_shim_binary_schemas(item) for item in value]
    return value


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: shim_openapi.py INPUT_OPENAPI_JSON OUTPUT_OPENAPI_JSON", file=sys.stderr)
        return 2

    source = Path(argv[1])
    target = Path(argv[2])
    spec = json.loads(source.read_text(encoding="utf-8"))
    shimmed = _shim_binary_schemas(spec)
    target.write_text(json.dumps(shimmed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
