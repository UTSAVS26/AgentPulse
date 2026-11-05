"""End-to-end check of the REAL integrations, regardless of ``MOCK_MODE``.

Exercises the same client classes the app uses:

1. Bifrost health and model listing.
2. A chat completion per model (one per provider by default).
3. A tool-calling round-trip (model must request the ``calculator`` tool).
4. A Tavily news search with dated sources.

Run inside the backend container so it uses the container's network and settings::

    docker compose exec backend python -m app.scripts.live_check
    docker compose exec backend python -m app.scripts.live_check --models groq/openai/gpt-oss-20b

Exit code is 0 only if every check passed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from app.clients.llm import BifrostClient, LLMError
from app.clients.search import SearchError, TavilySearchClient
from app.config import get_settings

DEFAULT_MODELS = [
    "groq/openai/gpt-oss-20b",
    "openrouter/nvidia/nemotron-3.5-lightning:free",
    "ollama/granite4:3b",
]
TOOL_MODEL = "groq/openai/gpt-oss-20b"
CALCULATOR_TOOL = {
    "type": "function",
    "function": {
        "name": "calculator",
        "description": "Evaluate an arithmetic expression exactly.",
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "e.g. 17 * 23"}},
            "required": ["expression"],
        },
    },
}

results: list[tuple[str, bool, str]] = []


def check(name: str, fn: Callable[[], str]) -> None:
    try:
        detail = fn()
        results.append((name, True, detail))
    except (LLMError, SearchError, AssertionError) as exc:
        results.append((name, False, str(exc)[:200]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--models", nargs="*", default=DEFAULT_MODELS)
    parser.add_argument("--tool-model", default=TOOL_MODEL)
    parser.add_argument("--skip-search", action="store_true")
    args = parser.parse_args()

    s = get_settings()
    llm = BifrostClient(s.bifrost_url, timeout_s=max(s.llm_timeout_s, 120))
    search = TavilySearchClient(s.tavily_api_key, timeout_s=s.search_timeout_s)

    def bifrost_health() -> str:
        ok, detail = llm.ping()
        assert ok, f"Bifrost /health failed: {detail}"
        return f"{s.bifrost_url} {detail}"

    def bifrost_models() -> str:
        models = llm.list_models()
        by_provider: dict[str, int] = {}
        for m in models:
            by_provider[m.split("/", 1)[0]] = by_provider.get(m.split("/", 1)[0], 0) + 1
        assert models, "no models returned"
        return ", ".join(f"{p}={n}" for p, n in sorted(by_provider.items()))

    check("bifrost: health", bifrost_health)
    check("bifrost: list models", bifrost_models)

    for model in args.models:

        def chat(model: str = model) -> str:
            r = llm.chat(model, [{"role": "user", "content": "Reply with exactly the word: pong"}])
            assert "pong" in r.content.lower(), f"unexpected reply: {r.content[:80]!r}"
            assert r.total_tokens > 0, "no token usage reported"
            return f"{r.content.strip()[:20]!r} · {r.total_tokens} tok · {r.latency_ms:.0f} ms · {r.provider}"

        check(f"chat: {model}", chat)

    def tool_call() -> str:
        r = llm.chat(
            args.tool_model,
            [{"role": "user", "content": "Use the calculator tool to compute 17 * 23."}],
            tools=[CALCULATOR_TOOL],
        )
        assert r.tool_calls, f"model did not call a tool (reply: {r.content[:80]!r})"
        call = r.tool_calls[0]
        assert call.name == "calculator", f"called {call.name}"
        return f"{call.name}({call.arguments}) · {r.latency_ms:.0f} ms"

    check(f"tool calling: {args.tool_model}", tool_call)

    if not args.skip_search:

        def tavily() -> str:
            r = search.search("current UK prime minister", topic="news", max_results=3)
            assert r.results, "no results"
            dated = [x for x in r.results if x.published_date]
            assert dated, "no result carried a published_date"
            newest = max(x.published_date for x in dated)
            return f"{len(r.results)} results, newest {newest} · {r.latency_ms:.0f} ms"

        check("tavily: news search", tavily)

    width = max(len(n) for n, _, _ in results)
    print()
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n  {len(results) - failed}/{len(results)} checks passed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
