"""Wait until a Docker container's HEALTHCHECK reports healthy.

    python scripts/wait_healthy.py <container> [timeout_seconds]

Exit 0 when healthy, 1 on timeout or if the container stops (its logs are printed).
"""

from __future__ import annotations

import subprocess
import sys
import time


def status(name: str) -> str:
    res = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}} {{.State.Health.Status}}", name],
        capture_output=True,
        text=True,
    )
    return res.stdout.strip() if res.returncode == 0 else "missing"


def main() -> int:
    name = sys.argv[1]
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 180
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = status(name)
        if last.endswith("healthy") and not last.endswith("unhealthy"):
            print(f"{name}: healthy")
            return 0
        if last.startswith(("exited", "dead", "missing")):
            break
        time.sleep(2)
    print(f"{name}: not healthy ({last})")
    subprocess.run(["docker", "logs", "--tail", "80", name])
    return 1


if __name__ == "__main__":
    sys.exit(main())
