"""Persisted notification outbox.

One card per qualifying job: the first flush sends it, later flushes edit the same message
when its rendered content changes. Failures back off and retry; a successful Gmail send is
already stored, so only the card update is retried. Paused → no new cards from ingestion;
digest mode → new cards are collected into one periodic summary instead.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Protocol

from jobradar.bot.cards import Reply, e, render_job_card
from jobradar.candidate import profile_status
from jobradar.context import AppContext
from jobradar.util import iso, utcnow

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 8


class Transport(Protocol):
    async def send(self, chat_id: int, reply: Reply) -> int: ...
    async def edit(self, chat_id: int, message_id: int, reply: Reply) -> None: ...


def is_paused(ctx: AppContext) -> bool:
    row = ctx.conn.execute("SELECT value FROM kv WHERE key = 'paused'").fetchone()
    return bool(row and row["value"] == "1")


def set_paused(ctx: AppContext, paused: bool) -> None:
    ctx.conn.execute("INSERT INTO kv VALUES ('paused', ?) ON CONFLICT(key) DO UPDATE SET "
                     "value = excluded.value", ("1" if paused else "0",))


async def flush(ctx: AppContext, transport: Transport, chat_id: int, limit: int | None = None
                ) -> dict:
    conn = ctx.conn
    limit = limit or ctx.settings.notify_max_per_minute
    paused = is_paused(ctx)
    digest = ctx.prefs.notify_mode == "digest"
    profile_ready = profile_status(conn)["ready_for_email"]
    counts = {"sent": 0, "edited": 0, "unchanged": 0, "failed": 0, "deferred": 0, "digest": 0}
    rows = conn.execute(
        "SELECT n.*, COALESCE(m.rank_score, 0) AS score FROM notifications n "
        "LEFT JOIN match_results m ON m.job_id = n.job_id AND m.invalidated_at IS NULL "
        "WHERE n.state = 'pending' AND n.next_attempt_at <= ? "
        "ORDER BY n.message_id IS NULL, score DESC LIMIT ?", (iso(), limit)).fetchall()
    for n in rows:
        if n["message_id"] is None and paused:
            counts["deferred"] += 1
            continue
        if n["message_id"] is None and digest:
            conn.execute("UPDATE notifications SET state = 'digest', updated_at = ? WHERE id = ?",
                         (iso(), n["id"]))
            counts["digest"] += 1
            continue
        reply = render_job_card(conn, n["job_id"], profile_ready)
        fp = reply.fingerprint()
        try:
            if n["message_id"] is None:
                mid = await transport.send(chat_id, reply)
                conn.execute("UPDATE notifications SET message_id = ?, chat_id = ?, "
                             "state = 'sent', rendered_hash = ?, attempts = 0, last_error = NULL, "
                             "updated_at = ? WHERE id = ?", (mid, chat_id, fp, iso(), n["id"]))
                counts["sent"] += 1
            elif fp == n["rendered_hash"]:
                conn.execute("UPDATE notifications SET state = 'sent', updated_at = ? WHERE id = ?",
                             (iso(), n["id"]))
                counts["unchanged"] += 1
            else:
                await transport.edit(n["chat_id"] or chat_id, n["message_id"], reply)
                conn.execute("UPDATE notifications SET state = 'sent', rendered_hash = ?, "
                             "attempts = 0, last_error = NULL, updated_at = ? WHERE id = ?",
                             (fp, iso(), n["id"]))
                counts["edited"] += 1
        except Exception as ex:
            if "message is not modified" in str(ex).lower():
                conn.execute("UPDATE notifications SET state = 'sent', rendered_hash = ? "
                             "WHERE id = ?", (fp, n["id"]))
                counts["unchanged"] += 1
                continue
            attempts = n["attempts"] + 1
            delay = timedelta(seconds=min(30 * 2 ** attempts, 3600))
            conn.execute(
                "UPDATE notifications SET attempts = ?, last_error = ?, next_attempt_at = ?, "
                "state = ?, updated_at = ? WHERE id = ?",
                (attempts, f"{type(ex).__name__}: {ex}"[:300], iso(utcnow() + delay),
                 "failed" if attempts >= MAX_ATTEMPTS else "pending", iso(), n["id"]))
            counts["failed"] += 1
            log.warning("notification for job %s failed: %s", n["job_id"], ex)
    return counts


def build_digest(ctx: AppContext) -> tuple[Reply | None, list[int]]:
    rows = ctx.conn.execute(
        "SELECT n.id, j.id AS job_id, j.title, j.publisher, m.decision, m.rank_score "
        "FROM notifications n JOIN jobs j ON j.id = n.job_id "
        "LEFT JOIN match_results m ON m.job_id = j.id AND m.invalidated_at IS NULL "
        "WHERE n.state = 'digest' ORDER BY m.rank_score DESC").fetchall()
    if not rows:
        return None, []
    lines = [f"<b>Job digest</b> — {len(rows)} new"]
    for r in rows:
        lines.append(f"#{r['job_id']} {e(r['title'])} — {e(r['publisher'])} "
                     f"({r['decision']}, {r['rank_score']}) · /job {r['job_id']}")
    return Reply("\n".join(lines)), [r["id"] for r in rows]


async def send_digest(ctx: AppContext, transport: Transport, chat_id: int) -> int:
    reply, ids = build_digest(ctx)
    if reply is None:
        return 0
    await transport.send(chat_id, reply)
    ctx.conn.executemany("UPDATE notifications SET state = 'sent', updated_at = ? WHERE id = ?",
                         [(iso(), i) for i in ids])
    return len(ids)
