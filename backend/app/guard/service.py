"""Freshness Guard core: forward a chat completion, check the answer for stale facts, then annotate, correct
or block it. Returns OpenAI-compatible JSON, so any OpenAI SDK, LangChain app or chat UI can use AgentPulse as
its ``base_url``.

Modes (header ``X-AgentPulse-Guard`` or body field ``guard``):

* ``annotate`` (default): the answer is unchanged; the response carries an ``agentpulse`` object and
  ``X-AgentPulse-*`` headers with the verdict, freshness and claims;
* ``correct``: a stale or wrong answer is rewritten from the dated sources, with citations; the original is
  kept in ``agentpulse.original``;
* ``block``: a stale or wrong answer is replaced by a warning listing what's outdated and what's true now;
* ``off``: plain proxy (no check).

Answers with no time-sensitive signal in the question or the answer (a poem, a definition) are not checked:
that keeps the proxy fast and free for most traffic.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.clients.llm import LLMClient, LLMError, LLMResponse
from app.clients.search import SearchClient
from app.config import Settings
from app.freshness.checker import FreshnessChecker, FreshnessResult, chat_with_fallback
from app.freshness.heuristics import temporal_signals
from app.freshness.store import save_check
from app.guard.prompts import CORRECT_MARKER

GuardMode = Literal["annotate", "correct", "block", "off"]
MODES = ("annotate", "correct", "block", "off")
FLAGGED = ("stale", "wrong")
CORRECT_SYSTEM = f"""{CORRECT_MARKER}

The input is JSON: today's date, the user's question, the original answer, and findings: each outdated or
wrong claim with what is true today and the dated sources that show it.
Rewrite the answer so it is correct as of today. Keep everything that was right, and the original tone and
length. Cite the sources inline as [1], [2]… (numbered as given) and end with a "Sources:" list of the cited
ones (title, date, URL). Return only the corrected answer, no preamble."""


@dataclass
class GuardOutcome:
    content: str
    action: str  # none | annotated | corrected | blocked | skipped
    agentpulse: dict[str, Any] = field(default_factory=dict)


def last_user_text(messages: list[dict[str, Any]]) -> str | None:
    """The newest user message as text (string content, or the text parts of a multi-part message)."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            return "\n".join(p for p in parts if p) or None
    return None


def needs_check(answer: str, question: str | None) -> bool:
    """Fast path: only answers that may contain time-sensitive facts are checked."""
    return bool(temporal_signals(answer) or (question and temporal_signals(question)))


def _sources(claim: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    return [
        {k: s.get(k) for k in ("title", "url", "published_date")}
        for s in sorted(
            claim.get("sources") or [], key=lambda s: s.get("published_date") or "", reverse=True
        )[:limit]
    ]


def findings(result: FreshnessResult) -> list[dict[str, Any]]:
    """The outdated or wrong claims with what's true today and their newest sources."""
    out = []
    for c in result.claims:
        if c.verdict in ("outdated", "contradicted"):
            d = c.to_dict()
            out.append(
                {
                    "claim": c.text,
                    "verdict": c.verdict,
                    "true_today": c.correction or c.explanation,
                    "sources": _sources(d),
                }
            )
    return out


def _numbered(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for f in found:
        for s in f["sources"]:
            if s.get("url") and s["url"] not in seen:
                seen[s["url"]] = s
    return list(seen.values())


def deterministic_correction(answer: str, found: list[dict[str, Any]]) -> str:
    """Correction without a model: the original answer plus a dated correction note per finding."""
    sources = _numbered(found)
    index = {s["url"]: i + 1 for i, s in enumerate(sources)}
    notes = []
    for f in found:
        refs = "".join(f"[{index[s['url']]}]" for s in f["sources"] if s.get("url") in index)
        what = "is outdated" if f["verdict"] == "outdated" else "is not correct"
        notes.append(f"- “{f['claim']}” {what}. True today: {f['true_today']} {refs}".rstrip())
    listing = "\n".join(
        f"[{i}] {s.get('title') or s['url']} ({s.get('published_date') or 'undated'}) {s['url']}"
        for i, s in enumerate(sources, start=1)
    )
    today = datetime.now(UTC).date().isoformat()
    return (
        f"{answer.strip()}\n\n⚠️ Correction (AgentPulse Freshness Guard, {today}):\n"
        + "\n".join(notes)
        + (f"\n\nSources:\n{listing}" if listing else "")
    )


def block_message(found: list[dict[str, Any]]) -> str:
    lines = [
        "⚠️ AgentPulse Freshness Guard withheld this answer: it contains facts that are no longer true or "
        "never were."
    ]
    for f in found:
        src = f["sources"][0] if f["sources"] else None
        cite = (
            f" (source: {src.get('title') or src.get('url')}, {src.get('published_date') or 'undated'})"
            if src
            else ""
        )
        lines.append(f"- “{f['claim']}”: {f['verdict']}. True today: {f['true_today']}{cite}")
    return "\n".join(lines)


class Guard:
    def __init__(self, llm: LLMClient, search: SearchClient, settings: Settings, *, live: bool):
        self.llm, self.search, self.settings, self.live = llm, search, settings, live

    def complete(self, body: dict[str, Any]) -> LLMResponse:
        max_tokens = body.get("max_completion_tokens") or body.get("max_tokens")
        temperature = body.get("temperature")
        return self.llm.chat(
            body["model"],
            body["messages"],
            tools=body.get("tools") or None,
            temperature=1.0 if temperature is None else float(temperature),
            max_tokens=int(max_tokens) if max_tokens else None,
        )

    def guard(self, db: Session, res: LLMResponse, messages: list[dict[str, Any]], mode: str) -> GuardOutcome:
        content = res.content or ""
        info: dict[str, Any] = {"guard_mode": mode, "checked": False}
        if mode == "off":
            return GuardOutcome(content, "none", {**info, "reason": "guard off"})
        if res.tool_calls or not content.strip():
            return GuardOutcome(content, "skipped", {**info, "reason": "no text answer (tool call)"})
        question = last_user_text(messages)
        if not needs_check(content, question):
            return GuardOutcome(content, "skipped", {**info, "reason": "nothing time-sensitive"})

        checker = FreshnessChecker(
            self.llm,
            self.search,
            self.settings.freshness_models,
            search_depth=self.settings.freshness_search_depth,
            retry_backoff_s=0.5 if self.live else 0.0,
        )
        result = checker.check(content, question=question)
        found = findings(result)
        flagged = result.verdict in FLAGGED and bool(found)
        action, new_content = "annotated", content
        if flagged and mode == "correct":
            new_content, how = self._correct(question, content, found)
            action = "corrected"
            info["correction"] = how
        elif flagged and mode == "block":
            new_content, action = block_message(found), "blocked"
        check = save_check(
            db,
            result,
            input_text=content,
            question=question,
            mock_mode=not self.live,
            source="guard",
            model=res.model,
            guard_action=action,
        )
        db.commit()
        info.update(
            {
                "checked": True,
                "check_id": check.id,
                "verdict": result.verdict,
                "freshness_score": result.freshness_score,
                "action": action,
                "check_latency_ms": round(result.latency_ms, 1),
                "claims": [
                    {
                        "text": c.text,
                        "verdict": c.verdict,
                        "confidence": c.confidence,
                        "true_today": c.correction,
                        "sources": _sources(c.to_dict()),
                    }
                    for c in result.claims
                ],
            }
        )
        if new_content != content:
            info["original"] = content
        return GuardOutcome(new_content, action, info)

    def _correct(self, question: str | None, answer: str, found: list[dict[str, Any]]) -> tuple[str, str]:
        sources = _numbered(found)
        payload = {
            "today": datetime.now(UTC).date().isoformat(),
            "question": question,
            "original_answer": answer,
            "findings": [
                {**f, "sources": [sources.index(s) + 1 for s in f["sources"] if s in sources]} for f in found
            ],
            "sources": [{"n": i, **s} for i, s in enumerate(sources, start=1)],
        }
        messages = [
            {"role": "system", "content": CORRECT_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            text = chat_with_fallback(
                self.llm,
                self.settings.freshness_models,
                messages,
                max_tokens=900,
                retry_backoff_s=0.5 if self.live else 0.0,
            ).strip()
            if text:
                return text, "rewritten by a model from the dated sources"
        except LLMError:
            pass
        return deterministic_correction(answer, found), "correction note appended (rewrite model unavailable)"


def completion_json(res: LLMResponse, content: str, request_model: str) -> dict[str, Any]:
    """An OpenAI ``chat.completion`` object."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if res.tool_calls:
        message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.raw_arguments or json.dumps(tc.arguments)},
            }
            for tc in res.tool_calls
        ]
        message["content"] = content or None
    return {
        "id": f"chatcmpl-agp-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": res.model or request_model,
        "choices": [{"index": 0, "message": message, "finish_reason": res.finish_reason or "stop"}],
        "usage": {
            "prompt_tokens": res.prompt_tokens,
            "completion_tokens": res.completion_tokens,
            "total_tokens": res.prompt_tokens + res.completion_tokens,
        },
    }


def sse_chunks(completion: dict[str, Any], agentpulse: dict[str, Any], piece: int = 48):
    """The (already guarded) answer as ``chat.completion.chunk`` server-sent events. The checked answer is
    buffered first, so streaming starts after the check: it is OpenAI-compatible, not lower-latency."""
    base = {k: completion[k] for k in ("id", "created", "model")} | {"object": "chat.completion.chunk"}
    message = completion["choices"][0]["message"]
    content = message.get("content") or ""

    def event(delta: dict[str, Any], finish: str | None = None, extra: dict[str, Any] | None = None) -> str:
        chunk = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **(extra or {})}
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    yield event({"role": "assistant", "content": ""})
    for i in range(0, len(content), piece):
        yield event({"content": content[i : i + piece]})
    if message.get("tool_calls"):
        yield event({"tool_calls": [{"index": i, **tc} for i, tc in enumerate(message["tool_calls"])]})
    yield event(
        {},
        completion["choices"][0]["finish_reason"],
        {"usage": completion["usage"], "agentpulse": agentpulse},
    )
    yield "data: [DONE]\n\n"
