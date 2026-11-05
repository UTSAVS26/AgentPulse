"""Validate bifrost/config.json against Bifrost's JSON schema.

    python scripts/validate_bifrost_config.py [config.json]      # validate
    python scripts/validate_bifrost_config.py --update-snapshot   # refresh the committed schema copy

The live schema (https://www.getbifrost.ai/schema) is used when reachable; otherwise the committed
snapshot ``bifrost/config.schema.json`` is used. Validation always runs. Exit code 1 if invalid.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import jsonschema

SCHEMA_URL = "https://www.getbifrost.ai/schema"
ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "bifrost" / "config.schema.json"


def download_schema() -> dict:
    # The schema host rejects Python's default User-Agent, so send an explicit one.
    req = urllib.request.Request(
        SCHEMA_URL, headers={"User-Agent": "agentpulse-ci/1.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as res:  # noqa: S310 - fixed https URL
        return json.load(res)


def load_schema() -> tuple[dict, str]:
    try:
        return download_schema(), SCHEMA_URL
    except Exception as exc:
        print(f"note: live schema unavailable ({exc}); using snapshot {SNAPSHOT.relative_to(ROOT)}")
        return json.loads(SNAPSHOT.read_text(encoding="utf-8")), str(SNAPSHOT.relative_to(ROOT))


def main(argv: list[str]) -> int:
    if argv[:1] == ["--update-snapshot"]:
        SNAPSHOT.write_text(json.dumps(download_schema(), indent=1) + "\n", encoding="utf-8")
        print(f"updated {SNAPSHOT.relative_to(ROOT)}")
        return 0

    path = Path(argv[0]) if argv else ROOT / "bifrost" / "config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {path} is not valid JSON: {exc}")
        return 1

    schema, source = load_schema()
    errors = sorted(jsonschema.Draft7Validator(schema).iter_errors(config), key=lambda e: list(e.path))
    if errors:
        print(f"ERROR: {path} has {len(errors)} schema error(s) (schema: {source}):")
        for e in errors:
            print(f"  - {'/'.join(map(str, e.path)) or '(root)'}: {e.message[:300]}")
        return 1
    print(f"OK: {path.name} is valid against {source} (providers: {', '.join(config.get('providers', {}))})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
