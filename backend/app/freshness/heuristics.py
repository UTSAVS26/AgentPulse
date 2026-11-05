"""Rule-based helpers for stale-answer detection (no LLM, no network).

Used in three places:

* as the **fallback claim extractor** when the extraction LLM fails or returns garbage;
* as an extra **time-sensitivity signal** on top of the LLM's own flag (a claim that says
  "current" or "latest" is always checked, whatever the LLM thinks);
* by the **mock** fact-checker, so mock mode exercises the same pipeline offline.

Time-sensitivity rules
----------------------
*Strong* signals make a claim time-sensitive on their own: words such as *current*, *latest*,
*now*, *today*, *incumbent*, *reigning*, *most recent*, *as of*, *this year*.

*Weak* signals (an office or title, a superlative or record, a price, a version number, a count
such as population) only count together with a **present-tense** verb (*is*, *are*, *holds*,
*has*...). "The tallest building is the Burj Khalifa" is time-sensitive; "The Berlin Wall fell
in 1989" is settled history; "Water boils at 100 °C" has no signal at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

STRONG_WORDS = (
    "current",
    "currently",
    "latest",
    "newest",
    "most recent",
    "right now",
    "nowadays",
    "today",
    "incumbent",
    "reigning",
    "defending champion",
    "as of",
    "this year",
    "this month",
    "this week",
    "at the moment",
    "at present",
    "presently",
    "so far",
    "to date",
)
ROLE_WORDS = (
    "prime minister",
    "president",
    "chancellor",
    "secretary-general",
    "secretary general",
    "ceo",
    "chief executive",
    "chair",
    "governor",
    "mayor",
    "king",
    "queen",
    "monarch",
    "pope",
    "leader",
    "head of",
    "champion",
    "winner",
    "won",
    "holds the record",
    "world record",
    "record holder",
)
SUPERLATIVE = re.compile(
    r"\b(tallest|largest|biggest|richest|fastest|most popular|most valuable|highest|longest|oldest living|"
    r"best-selling|top-selling)\b"
)
PRESENT = re.compile(r"\b(is|are|holds|hold|has|have|leads|lead|serves|runs|remains|costs|stands)\b")
YEAR = re.compile(r"\b(19|20)\d{2}\b")
VERSION = re.compile(
    r"\bv(ersion)?\s?\d+(\.\d+)+\b|\b\d+\.\d+(\.\d+)?\s(release|version)\b|"
    r"\b(python|node(\.js)?|java|ios|android|windows|macos|ubuntu|django|react|rust|kotlin|swift|php|ruby|"
    r"gpt|claude|gemini|llama|chrome|firefox) \d+(\.\d+)*\b"
)
PRICE = re.compile(r"[$€£¥]\s?\d|\b\d+(\.\d+)?\s?(usd|eur|dollars|euros|pounds)\b|\bprice\b|\bcosts?\b")
COUNT = re.compile(r"\b(population|subscribers|users|members|employees|market cap|valuation)\b")
HEDGE = re.compile(
    r"^(i'?m not (sure|certain)|i am not (sure|certain)|i don'?t (know|have)|i do not (know|have)|i cannot|"
    r"i can'?t|sorry|as an ai|i'?m unable|i am unable|unfortunately)",
    re.IGNORECASE,
)
FILLER = re.compile(r"^(the )?(final )?(answer|result)( is|:)\s*", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n+")
_ABBREV = re.compile(r"\b(Mr|Mrs|Ms|Dr|St|vs|e\.g|i\.e|U\.S|U\.K|No)\.$")


@dataclass
class HeuristicClaim:
    text: str
    time_sensitive: bool
    query: str
    signals: list[str] = field(default_factory=list)
    topic: str = "general"

    def to_dict(self) -> dict:
        return {
            "claim": self.text,
            "time_sensitive": self.time_sensitive,
            "query": self.query,
            "topic": self.topic,
            "signals": self.signals,
        }


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences (keeps abbreviations like "Dr." and decimals like 3.13 intact)."""
    text = re.sub(r"[*_`#>]+", " ", text or "")
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.MULTILINE)  # bullet markers
    parts: list[str] = []
    for chunk in _SENTENCE_SPLIT.split(text):
        chunk = re.sub(r"\s+", " ", chunk).strip()
        if not chunk:
            continue
        if parts and _ABBREV.search(parts[-1]):
            parts[-1] = f"{parts[-1]} {chunk}"
        else:
            parts.append(chunk)
    return parts


def temporal_signals(text: str) -> list[str]:
    """Why a sentence might be time-sensitive, e.g. ``["word:current", "role:prime minister"]``."""
    lower = f" {text.lower()} "
    found: list[str] = []
    found += [f"word:{w}" for w in STRONG_WORDS if re.search(rf"\b{re.escape(w)}\b", lower)]
    found += [f"role:{w}" for w in ROLE_WORDS if re.search(rf"\b{re.escape(w)}\b", lower)]
    found += [f"superlative:{m.group(1)}" for m in SUPERLATIVE.finditer(lower)]
    found += [f"count:{m.group(1)}" for m in COUNT.finditer(lower)]
    if PRICE.search(lower):
        found.append("price")
    if VERSION.search(lower):
        found.append("version")
    found += [f"year:{m.group()}" for m in YEAR.finditer(lower)]
    return list(dict.fromkeys(found))


def is_time_sensitive(text: str, signals: list[str] | None = None) -> bool:
    """Apply the strong/weak rules from the module docstring."""
    signals = temporal_signals(text) if signals is None else signals
    if any(s.startswith("word:") for s in signals):
        return True
    weak = [s for s in signals if not s.startswith("year:")]
    return bool(weak) and bool(PRESENT.search(text.lower()) or "role:won" in signals)


def strong_signal(text: str) -> bool:
    """True when the claim itself says it is about *now* (used to override an LLM's 'no')."""
    return any(s.startswith("word:") for s in temporal_signals(text))


def is_factual(sentence: str) -> bool:
    """Skip hedges, questions, fragments and pleasantries."""
    s = sentence.strip()
    if HEDGE.match(strip_filler(s)):  # also "The answer is I'm not sure."
        return False
    if len(s.split()) < 2 or s.endswith("?") or HEDGE.match(s):
        return False
    return bool(re.search(r"[A-Za-z]", s))


def strip_filler(answer: str) -> str:
    """Remove "The answer is" style lead-ins and trailing punctuation."""
    return FILLER.sub("", answer.strip()).rstrip(" .")


def heuristic_claims(text: str, question: str | None = None, max_claims: int = 8) -> list[HeuristicClaim]:
    """Extract claims without an LLM.

    With a ``question`` and a short answer (one or two sentences), the answer is treated as a
    single claim *in the context of the question*, so "The answer is Keir Starmer." to "Who is the
    current UK PM?" becomes a time-sensitive claim searched with the question itself.
    """
    sentences = [s for s in split_sentences(text) if is_factual(s)]
    if question and sentences and len(sentences) <= 2 and len(text) <= 300:
        answer = strip_filler(" ".join(sentences))
        claim = f"{answer} (answer to: {question.strip()})"
        signals = temporal_signals(f"{question} {answer}")
        return [
            HeuristicClaim(
                text=claim,
                time_sensitive=is_time_sensitive(f"{question} {answer}", signals),
                query=question.strip(),
                signals=signals,
            )
        ]
    claims = []
    for s in sentences[:max_claims]:
        signals = temporal_signals(s)
        claims.append(
            HeuristicClaim(
                text=s.rstrip(),
                time_sensitive=is_time_sensitive(s, signals),
                query=s.rstrip(" ."),
                signals=signals,
            )
        )
    return claims
