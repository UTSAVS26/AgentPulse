"""Freshness Guard demo: the plain OpenAI Python SDK, pointed at AgentPulse.

    pip install openai
    python scripts/guard_demo.py                       # local stack (make up / make up-live)
    python scripts/guard_demo.py --model groq/openai/gpt-oss-20b --question "Who won the latest World Cup?"

Environment: GUARD_BASE_URL (default http://localhost:8000/guard/v1), GUARD_API_KEY (the access code on a
public deployment; ignored locally).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - a hint for people running it without the SDK
    sys.exit("This demo needs the OpenAI SDK: pip install openai")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to a code page without emoji
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default="groq/openai/gpt-oss-20b")
    ap.add_argument(
        "--question", default="Who is the current prime minister of the United Kingdom? One sentence."
    )
    args = ap.parse_args()
    client = OpenAI(
        base_url=os.getenv("GUARD_BASE_URL", "http://localhost:8000/guard/v1"),
        api_key=os.getenv("GUARD_API_KEY", "not-needed"),
        timeout=180,
    )
    messages = [{"role": "user", "content": args.question}]
    print(f"Q: {args.question}\n   model: {args.model}\n")
    for mode in ("annotate", "correct", "block"):
        started = time.perf_counter()
        resp = client.chat.completions.create(
            model=args.model, messages=messages, extra_headers={"X-AgentPulse-Guard": mode}
        )
        report = resp.model_extra.get("agentpulse", {})
        took = time.perf_counter() - started
        print(
            f"── {mode} ({took:.1f} s) · verdict: {report.get('verdict', 'not checked')} · "
            f"action: {report.get('action', report.get('reason'))} · {report.get('mode')} mode"
        )
        if report.get("original"):
            print(f"   model said: {report['original']}")
        print(f"   app gets:   {resp.choices[0].message.content}\n")

    poem = client.chat.completions.create(
        model=args.model, messages=[{"role": "user", "content": "Write a two-line poem about rain."}]
    )
    print("── timeless question:", poem.model_extra.get("agentpulse", {}).get("reason"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
