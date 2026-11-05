"""Turn the Suites page's edited task table into API calls (pure functions, unit-tested).

``st.data_editor`` returns a DataFrame where new rows have no ``id`` and empty cells are ``None`` or NaN; the
diff against the original table gives the tasks to create, update and delete.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

COLUMNS = ["key", "prompt", "expected", "scorer", "tolerance", "category", "time_sensitive", "stale_answer"]


def _missing(v: Any) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA or v is pd.NaT


def _text(v: Any) -> str:
    return "" if _missing(v) else str(v).strip()


def clean_row(row: pd.Series | dict) -> dict:
    """One table row → a ``TaskIn`` payload with defaults for blank cells."""
    tol = row.get("tolerance")
    ts = row.get("time_sensitive")
    return {
        "key": _text(row.get("key")),
        "prompt": _text(row.get("prompt")),
        "expected": _text(row.get("expected")),
        "scorer": _text(row.get("scorer")) or "contains",
        "tolerance": 0.0 if _missing(tol) else float(tol),
        "category": _text(row.get("category")) or "general",
        "time_sensitive": False if _missing(ts) else bool(ts),
        "stale_answer": _text(row.get("stale_answer")) or None,
    }


def diff_tasks(
    before: pd.DataFrame, after: pd.DataFrame
) -> tuple[list[dict], list[tuple[int, dict]], list[int]]:
    """Return ``(creates, updates as (task_id, payload), deletes as task ids)``.

    Rows without an id are new (fully blank ones are ignored); rows whose cleaned values changed are updates;
    original ids missing from ``after`` are deletes."""
    old = {int(r["id"]): clean_row(r) for _, r in before.iterrows()}
    creates: list[dict] = []
    updates: list[tuple[int, dict]] = []
    kept: set[int] = set()
    for _, r in after.iterrows():
        data = clean_row(r)
        rid = r.get("id")
        if _missing(rid):
            if data["key"] or data["prompt"]:
                creates.append(data)
            continue
        kept.add(int(rid))
        if data != old.get(int(rid)):
            updates.append((int(rid), data))
    return creates, updates, [i for i in old if i not in kept]
