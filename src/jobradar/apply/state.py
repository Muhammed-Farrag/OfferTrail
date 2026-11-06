"""Application state machine. Every transition is checked and logged in `status_events`."""

from __future__ import annotations

import sqlite3

from jobradar.util import iso

DISPLAY = {
    "needs_review": "🔎 Needs review",
    "ready_for_approval": "✉️ Ready for approval",
    "sending": "⏳ Sending",
    "applied_gmail": "✅ Applied via Gmail",
    "manual_action": "📝 Manual action needed",
    "applied_manual": "✅ Applied manually (self-reported)",
    "send_unknown": "⚠️ Send uncertain — verify Sent mail",
    "send_failed": "❌ Send failed",
    "skipped": "⏭️ Skipped",
}

TRANSITIONS: dict[str, set[str]] = {
    "needs_review": {"needs_review", "ready_for_approval", "manual_action", "skipped",
                     "applied_manual"},
    "manual_action": {"manual_action", "needs_review", "ready_for_approval", "applied_manual",
                      "skipped"},
    "ready_for_approval": {"ready_for_approval", "sending", "needs_review", "manual_action",
                           "skipped"},
    "sending": {"applied_gmail", "send_failed", "send_unknown"},
    "send_unknown": {"applied_manual", "send_failed"},
    "send_failed": {"ready_for_approval", "needs_review", "manual_action", "skipped",
                    "applied_manual"},
    "skipped": {"needs_review", "manual_action"},
    "applied_gmail": set(),
    "applied_manual": set(),
}
APPLIED = {"applied_gmail", "applied_manual"}
IN_FLIGHT = {"sending", "send_unknown"}


class InvalidTransition(Exception):
    pass


def transition(conn: sqlite3.Connection, app_id: int, to_state: str, actor: str,
               reason: str | None = None, expected_from: set[str] | None = None) -> str:
    """Change state inside the caller's transaction. Returns the previous state."""
    row = conn.execute("SELECT job_id, state FROM applications WHERE id = ?", (app_id,)).fetchone()
    if row is None:
        raise InvalidTransition(f"application {app_id} not found")
    cur = row["state"]
    if expected_from is not None and cur not in expected_from:
        raise InvalidTransition(f"application is {cur}, expected one of {sorted(expected_from)}")
    if to_state not in TRANSITIONS[cur]:
        raise InvalidTransition(f"cannot move from {cur} to {to_state}")
    now = iso()
    conn.execute("UPDATE applications SET state = ?, state_reason = ?, updated_at = ? "
                 "WHERE id = ?", (to_state, reason, now, app_id))
    if cur != to_state or reason:
        conn.execute(
            "INSERT INTO status_events (job_id, application_id, from_state, to_state, actor, "
            "detail, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["job_id"], app_id, cur, to_state, actor, reason, now))
    return cur


def get_application(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
