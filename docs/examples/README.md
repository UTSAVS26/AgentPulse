# Example reports

Exported with `GET /reports/{run_id}` (or the **📄 Reports** page) from a **live** run on 2026-11-05: the
Current Events suite, 2 Groq agents through Bifrost, answers verified with Tavily.

| File | Format |
|---|---|
| [live-run-report.md](live-run-report.md) | Markdown report |
| [live-run-leaderboard.csv](live-run-leaderboard.csv) | Leaderboard CSV (raw numbers: ratios 0–1, ms, USD) |

Notable in this run: both agents answered **Python 3.14** to "latest stable Python" (3.15 shipped in October
2026), and the detector flagged both as stale, citing the release announcement.
