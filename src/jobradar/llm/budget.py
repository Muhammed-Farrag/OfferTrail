"""Daily token/request budget for the free LLM tier, persisted in SQLite."""

from __future__ import annotations

import sqlite3

from jobradar.util import utcnow


def _today() -> str:
    return utcnow().strftime("%Y-%m-%d")


def usage_today(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM llm_usage WHERE day = ?", (_today(),)).fetchone()
    return dict(row) if row else {"day": _today(), "requests": 0, "tokens": 0, "errors": 0,
                                  "last_error": None}


def has_budget(conn: sqlite3.Connection, daily_tokens: int) -> bool:
    return usage_today(conn)["tokens"] < daily_tokens


def record(conn: sqlite3.Connection, tokens: int = 0, error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO llm_usage (day, requests, tokens, errors, last_error) VALUES (?, 1, ?, ?, ?) "
        "ON CONFLICT(day) DO UPDATE SET requests = requests + 1, "
        "tokens = tokens + excluded.tokens, errors = errors + excluded.errors, "
        "last_error = COALESCE(excluded.last_error, last_error)",
        (_today(), tokens, 1 if error else 0, error),
    )
