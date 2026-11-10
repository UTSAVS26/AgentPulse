"""Event-age buckets of the Live Freshness Bench and the categories it samples.

Ages are measured in days before the snapshot date. The buckets are narrow near "today" (where models'
knowledge ends) and wider in the past (where it should be stable).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Bucket:
    key: str
    label: str
    min_days: int
    max_days: int

    def window(self, today: date) -> tuple[date, date]:
        """The publication window ``(start, end)`` of this bucket, as of ``today``."""
        return today - timedelta(days=self.max_days), today - timedelta(days=self.min_days)

    def contains(self, age_days: int) -> bool:
        return self.min_days <= age_days <= self.max_days

    @property
    def mid_days(self) -> float:
        return (self.min_days + self.max_days) / 2


BUCKETS: tuple[Bucket, ...] = (
    Bucket("week", "≤ 7 days", 0, 7),
    Bucket("month", "8–30 days", 8, 30),
    Bucket("quarter", "1–3 months", 31, 91),
    Bucket("half", "3–6 months", 92, 182),
    Bucket("year", "6–12 months", 183, 365),
    Bucket("older", "1–2 years", 366, 730),
)
BUCKET_KEYS = tuple(b.key for b in BUCKETS)

CATEGORIES: dict[str, str] = {
    # category → the neutral news query sent to Tavily (with a publication-date window)
    "politics": "election result winner announced",
    "sports": "championship final winner",
    "technology": "company launches new product",
    "business": "company agrees to acquire deal",
    "science": "researchers discover study published",
    "world": "international agreement signed summit",
}


def bucket_for(age_days: int) -> Bucket | None:
    return next((b for b in BUCKETS if b.contains(age_days)), None)


def age_days(event_date: str | date, reference: str | date) -> int:
    """Whole days from ``event_date`` to ``reference`` (both ``YYYY-MM-DD`` or dates)."""
    ev = date.fromisoformat(event_date) if isinstance(event_date, str) else event_date
    ref = date.fromisoformat(reference) if isinstance(reference, str) else reference
    return (ref - ev).days
