"""Assemble the Hugging Face Space folder (default: dist/space).

    python scripts/build_space.py [out_dir]

The Space gets only what it needs to run: backend app + requirements, frontend pages, the
cloud-only Bifrost config, and deploy/space/{Dockerfile,start.sh,README.md}. The same folder is
built and smoke-tested in CI, then uploaded by scripts/deploy_space.py.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "tests", ".ruff_cache")

# (source, destination inside the Space)
FILES = [
    ("deploy/space/Dockerfile", "Dockerfile"),
    ("deploy/space/start.sh", "start.sh"),
    ("deploy/space/README.md", "README.md"),
    ("bifrost/config.space.json", "bifrost/config.json"),
    ("backend/requirements.txt", "backend/requirements.txt"),
    ("frontend/requirements.txt", "frontend/requirements.txt"),
    ("frontend/app.py", "frontend/app.py"),
    ("frontend/api.py", "frontend/api.py"),
    ("frontend/ui.py", "frontend/ui.py"),
]
DIRS = [
    ("backend/app", "backend/app"),
    ("frontend/pages", "frontend/pages"),
]


def build(out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for src, dst in FILES:
        target = out / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        data = (ROOT / src).read_bytes().replace(b"\r\n", b"\n")  # LF only (start.sh runs in bash)
        target.write_bytes(data)
    for src, dst in DIRS:
        shutil.copytree(ROOT / src, out / dst, ignore=IGNORE)
    return out


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "space"
    build(out)
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    print(f"built {out} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
