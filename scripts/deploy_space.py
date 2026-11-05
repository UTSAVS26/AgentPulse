"""Deploy AgentPulse to a Hugging Face Space and wait until it is running.

Environment:
  HF_TOKEN        Hugging Face token with WRITE access (required)
  HF_SPACE        Space id, e.g. "UTSAVS26/agentpulse" (required)
  GROQ_API_KEY, OPENROUTER_API_KEY, TAVILY_API_KEY, LIVE_ACCESS_CODE, DATABASE_URL
                  Optional: synced to the Space as *secrets* when set (never uploaded as files).
  GIT_SHA         Optional: recorded in the Space commit message.

Steps: build dist/space → create the Space if missing (Docker SDK) → sync secrets → upload the
folder (deleting stale files) → wait for the Space to be RUNNING → check the public URL.
Exits non-zero on build/runtime errors so the GitHub Actions run fails visibly.
"""

from __future__ import annotations

import os
import re
import sys
import time
import urllib.request
from pathlib import Path

from huggingface_hub import HfApi

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_space import ROOT, build  # noqa: E402

SECRETS = ["GROQ_API_KEY", "OPENROUTER_API_KEY", "TAVILY_API_KEY", "LIVE_ACCESS_CODE", "DATABASE_URL"]
FAILED_STAGES = {"BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR", "NO_APP_FILE"}
WAIT_S = int(os.getenv("DEPLOY_WAIT_S", "1500"))


def space_url(space_id: str) -> str:
    """https://<owner>-<name>.hf.space (lowercase; dots/underscores become dashes)."""
    return "https://" + re.sub(r"[^a-z0-9-]", "-", space_id.replace("/", "-").lower()) + ".hf.space"


def main() -> int:
    token, space_id = os.getenv("HF_TOKEN"), os.getenv("HF_SPACE")
    if not token or not space_id:
        print("::notice::HF_TOKEN / HF_SPACE not configured; skipping deployment.")
        return 0

    api = HfApi(token=token)
    folder = build(ROOT / "dist" / "space")
    print(f"built {folder}")

    api.create_repo(space_id, repo_type="space", space_sdk="docker", exist_ok=True)
    synced = []
    for key in SECRETS:
        value = os.getenv(key)
        if value:
            api.add_space_secret(space_id, key, value)
            synced.append(key)
    print(f"secrets synced: {', '.join(synced) or 'none (public mock-only demo)'}")

    sha = os.getenv("GIT_SHA", "local")[:12]
    api.upload_folder(
        repo_id=space_id,
        repo_type="space",
        folder_path=str(folder),
        commit_message=f"Deploy AgentPulse {sha}",
        delete_patterns=["*", "**/*"],  # mirror the folder exactly (remove stale files)
    )
    print(f"uploaded to https://huggingface.co/spaces/{space_id}")

    deadline = time.monotonic() + WAIT_S
    stage = ""
    while time.monotonic() < deadline:
        stage = str(api.get_space_runtime(space_id).stage)
        stage = stage.split(".")[-1]  # SpaceStage.RUNNING -> RUNNING
        print(f"space stage: {stage}")
        if stage in FAILED_STAGES:
            print(
                f"::error::Space failed with stage {stage}. See the logs at https://huggingface.co/spaces/{space_id}?logs=container"
            )
            return 1
        if stage == "RUNNING":
            break
        time.sleep(20)
    else:
        print(f"::error::Space not RUNNING after {WAIT_S}s (last stage: {stage})")
        return 1

    url = space_url(space_id)
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{url}/_stcore/health", timeout=10) as res:
                if res.status == 200:
                    print(f"::notice::AgentPulse is live at {url}")
                    if os.getenv("GITHUB_OUTPUT"):
                        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
                            fh.write(f"url={url}\n")
                    return 0
        except Exception as exc:  # the Space may still be warming up
            print(f"waiting for {url}: {exc}")
        time.sleep(10)
    print(f"::error::Space is RUNNING but {url}/_stcore/health did not respond")
    return 1


if __name__ == "__main__":
    sys.exit(main())
