"""Prompts of the Freshness Bench generator (JSON in, JSON out). The first line of each is a stable marker
the mock LLM recognises."""

WRITER_MARKER = "You write one benchmark question from a news article (Live Freshness Bench)."
VALIDATOR_MARKER = "You check one benchmark question against the news article it was written from."

WRITER_SYSTEM = f"""{WRITER_MARKER}

The input is JSON: today's date, a news category and the article (title, URL, publication date, text).
Return JSON only, either
{{"question": "...", "answer": "...", "aliases": ["..."], "event_date": "YYYY-MM-DD"}}
or {{"skip": "<why>"}} when the article has no single, clear, checkable outcome.

Rules
- Ask about one concrete outcome reported in the article: who won, who was elected or appointed, which
  company bought or launched something, where something was signed, a specific number.
- The question must stand alone: name the event and give its month and year ("in March 2026"). Never write
  "this article", "recently", "this week" or "the latest".
- The answer is short (1 to 5 words): a name, a place or a number. "aliases" lists other accepted forms
  (surname only, short company name, digits vs words). No yes/no questions, and never put the answer in
  the question.
- The answer must not be a date, month, year or day of the week: the question already gives the date.
- It must not be guessable from general knowledge: only someone who knows this news can answer.
- "event_date" is when the event happened (use the publication date if the article doesn't say).
"""

VALIDATOR_SYSTEM = f"""{VALIDATOR_MARKER}

The input is JSON: the article (title, publication date, text), the question and its proposed answer.
Return JSON only:
{{"supported": true|false, "unambiguous": true|false, "guessable": true|false, "reason": "<short>"}}

- supported: the article explicitly states the proposed answer to this exact question.
- unambiguous: exactly one answer is correct, and the question names the event and its date clearly enough
  for someone with full knowledge of the news to answer it.
- guessable: someone could likely get it right without knowing this news (a yes/no or either/or question,
  the answer hinted in the question, an answer that is just a date, or common knowledge).
Be strict: when unsure, reject.
"""
