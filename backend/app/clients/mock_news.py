"""A fictional, dated news corpus for the Live Freshness Bench in ``MOCK_MODE``.

Every team, company, person and place here is invented, so the offline demo never states anything about the
real world. Dates are relative to *today*, so the mock bench never goes stale. A few items carry a deliberate
flaw (``unsupported`` or ``ambiguous``) that the mock validator rejects, which shows question validation at
work.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

from app.freshbench.buckets import BUCKETS, CATEGORIES

PER_CELL = 3  # items per category × age bucket


def today() -> date:
    """'Today' for the mock corpus (UTC); tests monkeypatch this."""
    return datetime.now(UTC).date()


@dataclass(frozen=True)
class NewsItem:
    category: str
    published: str  # YYYY-MM-DD
    title: str
    content: str
    url: str
    question: str
    answer: str  # "Answer|Alias"
    anchor: str  # unique phrase that identifies the event in a question
    flaw: str | None = None  # "unsupported" | "ambiguous": the validator rejects these


_A = [
    "Kestrel",
    "Brightmoor",
    "Ashgrove",
    "Halden",
    "Quillon",
    "Marrow",
    "Tessaly",
    "Corwin",
    "Ellery",
    "Fenwick",
    "Larkspur",
    "Oakhurst",
    "Pellam",
    "Ravelin",
    "Stellan",
    "Thornby",
    "Verity",
    "Wyncote",
    "Calder",
    "Dunmore",
]
_B = [
    "Falcons",
    "Mariners",
    "Comets",
    "Rovers",
    "Harriers",
    "Lynx",
    "Pioneers",
    "Tempest",
    "Wardens",
    "Kites",
    "Stags",
    "Herons",
]
_TECH = ["Dynamics", "Labs", "Systems", "Robotics", "Optics", "Networks", "Quantum", "Devices"]
_PRODUCTS = ["Halo", "Arc", "Pulse", "Nimbus", "Vector", "Prism", "Orbit", "Lumen", "Cinder", "Atlas"]
_DEVICES = ["tablet", "headset", "drone", "e-reader", "smart ring", "home robot", "laptop", "camera"]
_FIRST = [
    "Mara",
    "Tobin",
    "Iris",
    "Callum",
    "Noor",
    "Elias",
    "Petra",
    "Ansel",
    "Lena",
    "Rafael",
    "Odile",
    "Kenji",
    "Saoirse",
    "Matteo",
    "Ines",
    "Dorian",
]
_THINGS = [
    "a deep-sea bioluminescent coral",
    "a cold-tolerant wheat gene",
    "a new exoplanet ring system",
    "an enzyme that breaks down nylon",
    "a fossil feathered reptile",
    "a room-temperature salt battery",
    "a glacier microbe that eats methane",
    "a fast-folding protein switch",
    "a comet with two tails",
    "a drought-resistant rice strain",
    "a magnetic bacteria colony",
    "an ancient river delta on Mars",
    "a self-healing concrete fungus",
    "a frog that glows under UV light",
    "a twin-star planet nursery",
    "a coral that survives heatwaves",
    "a moon with salt geysers",
    "a beetle that walks underwater",
    "a pollen-free tomato variety",
    "a bacterium that stores hydrogen",
]
_LANDS = ["Valdoria", "Estmark", "Corvane", "Lysander Isles", "Ostrava Union", "Meridia", "Tarsis", "Nordhal"]
_TREATIES = [
    "Clearwater Accord",
    "Highland Fisheries Pact",
    "Amber Coast Treaty",
    "Skyline Trade Charter",
    "Riverstone Water Agreement",
    "Silverline Rail Pact",
    "Greenbelt Climate Accord",
    "Harbor Light Compact",
]


def _month_year(d: date) -> str:
    return d.strftime("%B %Y")


def _item(category: str, d: date, i: int, rng: random.Random) -> NewsItem:
    """One fictional event of ``category`` dated ``d`` (``i`` keeps anchors unique within the corpus)."""
    when = _month_year(d)
    if category == "sports":
        team, rival = f"{_A[i % 20]} {_B[i % 12]}", f"{_A[(i + 7) % 20]} {_B[(i + 5) % 12]}"
        cup = f"{_A[(i + 3) % 20]} Cup"
        title = f"{team} win the {cup} final"
        score = f"{rng.randint(1, 4)}-{rng.randint(0, 1)}"
        content = (
            f"The {team} beat the {rival} {score} in the {cup} final, lifting the trophy for the first time."
        )
        question, answer, anchor = f"Which team won the {cup} final in {when}?", f"{team}|{_B[i % 12]}", cup
    elif category == "technology":
        company = f"{_A[i % 20]} {_TECH[i % 8]}"
        product = f"{_PRODUCTS[i % 10]} {_DEVICES[(i + 3) % 8]}"
        title = f"{company} launches the {product}"
        content = f"{company} unveiled the {product} at a launch event, with shipping planned within weeks."
        question, answer, anchor = (
            f"Which company launched the {product} in {when}?",
            f"{company}|{_A[i % 20]}",
            product,
        )
    elif category == "business":
        buyer, target = f"{_A[i % 20]} Holdings", f"{_A[(i + 9) % 20]} {_TECH[(i + 2) % 8]}"
        title = f"{buyer} agrees to buy {target}"
        content = (
            f"{buyer} has agreed to acquire {target} for ${rng.randint(2, 40)} billion in cash and stock."
        )
        question, answer, anchor = (
            f"Which company agreed in {when} to acquire {target}?",
            f"{buyer}|{_A[i % 20]}",
            target,
        )
    elif category == "politics":
        person = f"{_FIRST[i % 16]} {_A[(i + 11) % 20]}"
        city = f"Port {_A[(i + 4) % 20]}"
        title = f"{person} elected mayor of {city}"
        content = f"{person} won the mayoral election in {city} with {rng.randint(51, 64)}% of the vote."
        question, answer, anchor = (
            f"Who was elected mayor of {city} in {when}?",
            f"{person}|{_A[(i + 11) % 20]}",
            city,
        )
    elif category == "science":
        institute = f"{_A[i % 20]} Institute"
        thing = _THINGS[i % len(_THINGS)]
        title = f"{institute} researchers report {thing}"
        content = f"Researchers at the {institute} published a study describing {thing}."
        question, answer, anchor = (
            f"Researchers at which institute reported {thing} in {when}?",
            f"{institute}|{_A[i % 20]}",
            thing,
        )
    else:  # world
        treaty = f"{('', 'Second ', 'Third ')[i // 8]}{_TREATIES[i % 8]}".strip()
        host = f"{_A[(i + 6) % 20]}ford"
        lands = f"{_LANDS[i % 8]} and {_LANDS[(i + 3) % 8]}"
        title = f"{lands} sign the {treaty}"
        content = f"Leaders of {lands} signed the {treaty} at a summit held in {host}."
        question, answer, anchor = f"In which city was the {treaty} signed in {when}?", host, treaty
    flaw = None
    if i % 9 == 4:
        flaw = "unsupported"  # the question asks something the article doesn't state
        question = question.replace(" in ", " for the second time in ", 1)
    elif i % 9 == 7:
        flaw = "ambiguous"
    slug = "-".join("".join(c if c.isalnum() else " " for c in title.lower()).split())[:70]
    return NewsItem(
        category=category,
        published=d.isoformat(),
        title=f"{title} (simulated)",
        content=content,
        url=f"https://mock.agentpulse.local/news/{d.isoformat()}/{slug}",
        question=question,
        answer=answer,
        anchor=anchor,
        flaw=flaw,
    )


@lru_cache(maxsize=4)
def corpus(today: date) -> tuple[NewsItem, ...]:
    """The corpus as of ``today``: ``PER_CELL`` items per category and age bucket."""
    items: list[NewsItem] = []
    for ci, category in enumerate(CATEGORIES):
        for bi, bucket in enumerate(BUCKETS):
            for k in range(PER_CELL):
                i = bi * PER_CELL + k + ci  # unique within a category
                rng = random.Random(f"{category}|{bucket.key}|{k}")
                age = rng.randint(bucket.min_days, bucket.max_days)
                items.append(_item(category, today - timedelta(days=age), i, rng))
    return tuple(items)


def search_window(query: str, start: str | None, end: str | None, today: date) -> list[NewsItem]:
    """News items whose category matches the query and whose date is inside ``[start, end]``."""
    q = query.lower()
    cats = [c for c, seed in CATEGORIES.items() if c in q or seed in q] or list(CATEGORIES)
    return [
        it
        for it in corpus(today)
        if it.category in cats and (not start or it.published >= start) and (not end or it.published <= end)
    ]


def find_item(text: str, today: date) -> NewsItem | None:
    """The item a question (or search query) is about, matched on its unique anchor phrase."""
    low = text.lower()
    hits = [it for it in corpus(today) if it.anchor.lower() in low]
    return max(hits, key=lambda it: len(it.anchor), default=None)


def find_by_url(url: str, today: date) -> NewsItem | None:
    return next((it for it in corpus(today) if it.url == url), None)
