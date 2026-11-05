"""The Suites page's table diff (no backend needed)."""

import math

import pandas as pd

from suite_edit import COLUMNS, clean_row, diff_tasks


def frame(rows):
    return pd.DataFrame(rows, columns=["id", *COLUMNS, "used"])


BASE = [
    {
        "id": 1,
        "key": "a",
        "prompt": "P1",
        "expected": "x",
        "scorer": "contains",
        "tolerance": 0.0,
        "category": "general",
        "time_sensitive": False,
        "stale_answer": None,
        "used": 0,
    },
    {
        "id": 2,
        "key": "b",
        "prompt": "P2",
        "expected": "5",
        "scorer": "numeric",
        "tolerance": 0.5,
        "category": "math",
        "time_sensitive": True,
        "stale_answer": "4",
        "used": 3,
    },
]


def test_no_changes_means_nothing_to_save():
    assert diff_tasks(frame(BASE), frame(BASE)) == ([], [], [])


def test_create_update_delete_detected():
    after = frame(
        [
            {**BASE[0], "expected": "x|y"},  # edited
            # row 2 deleted
            {
                "id": float("nan"),
                "key": "c",
                "prompt": "P3",
                "expected": None,
                "scorer": None,
                "tolerance": float("nan"),
                "category": None,
                "time_sensitive": None,
                "stale_answer": None,
                "used": None,
            },
            {"id": None, "key": None, "prompt": None},  # blank new row: ignored
        ]
    )
    creates, updates, deletes = diff_tasks(frame(BASE), after)
    assert creates == [
        {
            "key": "c",
            "prompt": "P3",
            "expected": "",
            "scorer": "contains",
            "tolerance": 0.0,
            "category": "general",
            "time_sensitive": False,
            "stale_answer": None,
        }
    ]
    assert updates == [(1, {**clean_row(BASE[0]), "expected": "x|y"})]
    assert deletes == [2]


def test_whitespace_and_nan_do_not_count_as_edits():
    after = frame([{**BASE[0], "key": " a ", "stale_answer": float("nan")}, BASE[1]])
    assert diff_tasks(frame(BASE), after) == ([], [], [])


def test_clean_row_defaults():
    row = clean_row({"key": "k", "prompt": "p", "tolerance": float("nan"), "time_sensitive": float("nan")})
    assert row["scorer"] == "contains" and row["category"] == "general" and row["tolerance"] == 0.0
    assert row["time_sensitive"] is False and row["stale_answer"] is None
    assert not math.isnan(row["tolerance"])
