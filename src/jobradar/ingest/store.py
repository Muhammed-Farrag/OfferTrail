"""Durable ingestion: persist raw messages first, process them afterwards.

A message is written to `source_posts` before the channel cursor moves past it, so a crash
between the two at worst re-delivers a message (deduplicated by content hash) and never
drops one. Processing failures are recorded per post and retried independently.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from jobradar.db.conn import transaction
from jobradar.util import dumps, iso, sha256_text


@dataclass
class RawMessage:
    channel_id: str
    message_id: int
    text: str
    published_at: datetime
    links: list[dict] = field(default_factory=list)  # [{"text": ..., "url": ...}]
    edited_at: datetime | None = None
    channel_username: str | None = None

    @property
    def permalink(self) -> str | None:
        if self.channel_username:
            return f"https://t.me/{self.channel_username}/{self.message_id}"
        return None

    def content_hash(self) -> str:
        return sha256_text(self.text, dumps(self.links))


@dataclass
class StoreResult:
    post_id: int
    status: str  # new | unchanged | edited


def store_message(conn: sqlite3.Connection, msg: RawMessage) -> StoreResult:
    h = msg.content_hash()
    with transaction(conn, immediate=True):
        cur = conn.execute(
            "SELECT id, version, content_hash FROM source_posts "
            "WHERE channel_id = ? AND message_id = ? AND is_current = 1",
            (msg.channel_id, msg.message_id),
        ).fetchone()
        if cur and cur["content_hash"] == h:
            return StoreResult(cur["id"], "unchanged")
        version = 1
        if cur:
            version = cur["version"] + 1
            conn.execute("UPDATE source_posts SET is_current = 0 WHERE id = ?", (cur["id"],))
        post_id = conn.execute(
            "INSERT INTO source_posts (channel_id, message_id, version, is_current, permalink, "
            "raw_text, links_json, published_at, edited_at, content_hash, ingested_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (msg.channel_id, msg.message_id, version, msg.permalink, msg.text, dumps(msg.links),
             iso(msg.published_at), iso(msg.edited_at) if msg.edited_at else None, h, iso()),
        ).lastrowid
        if cur:
            # Re-point job links to the new version and invalidate analysis built on the old one.
            jobs = [r["job_id"] for r in conn.execute(
                "SELECT job_id FROM job_sources WHERE post_id = ?", (cur["id"],))]
            for job_id in jobs:
                conn.execute("INSERT OR IGNORE INTO job_sources VALUES (?, ?, ?)",
                             (job_id, post_id, iso()))
                invalidate_job_analysis(conn, job_id, f"source post {msg.message_id} edited")
        return StoreResult(post_id, "edited" if cur else "new")


def invalidate_job_analysis(conn: sqlite3.Connection, job_id: int, reason: str) -> None:
    """Invalidate the current match result and any approval that is not yet used.

    Must be called inside a transaction. Application history (sent attempts, applied states)
    is never touched.
    """
    now = iso()
    conn.execute(
        "UPDATE match_results SET invalidated_at = ?, invalidation_reason = ? "
        "WHERE job_id = ? AND invalidated_at IS NULL", (now, reason, job_id))
    conn.execute(
        "UPDATE approvals SET invalidated_at = ?, invalidation_reason = ? "
        "WHERE invalidated_at IS NULL AND draft_id IN ("
        "  SELECT d.id FROM drafts d JOIN applications a ON a.id = d.application_id "
        "  WHERE a.job_id = ? AND a.state IN ('needs_review', 'ready_for_approval'))",
        (now, reason, job_id))
    app = conn.execute(
        "SELECT id, state FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    if app and app["state"] == "ready_for_approval":
        conn.execute(
            "UPDATE applications SET state = 'needs_review', state_reason = ?, updated_at = ? "
            "WHERE id = ?", (f"approval invalidated: {reason}", now, app["id"]))
        conn.execute(
            "INSERT INTO status_events (job_id, application_id, from_state, to_state, actor, "
            "detail, at) VALUES (?, ?, 'ready_for_approval', 'needs_review', 'system', ?, ?)",
            (job_id, app["id"], reason, now))


def advance_cursor(conn: sqlite3.Connection, channel_id: str, message_id: int,
                   report: dict | None = None) -> None:
    """Move the cursor forward (never backward). Call only after messages are stored."""
    with transaction(conn, immediate=True):
        row = conn.execute("SELECT last_message_id FROM ingest_cursors WHERE channel_id = ?",
                           (channel_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO ingest_cursors VALUES (?, ?, ?, ?)",
                         (channel_id, message_id, iso(), dumps(report) if report else None))
        else:
            conn.execute(
                "UPDATE ingest_cursors SET last_message_id = MAX(last_message_id, ?), "
                "updated_at = ?, last_run_json = COALESCE(?, last_run_json) WHERE channel_id = ?",
                (message_id, iso(), dumps(report) if report else None, channel_id))


def get_cursor(conn: sqlite3.Connection, channel_id: str) -> int | None:
    row = conn.execute("SELECT last_message_id FROM ingest_cursors WHERE channel_id = ?",
                       (channel_id,)).fetchone()
    return row["last_message_id"] if row else None


def ingest_batch(conn: sqlite3.Connection, messages: list[RawMessage],
                 report: dict | None = None) -> dict:
    """Store a batch (any order) then advance the cursor to the highest stored ID."""
    counts = {"new": 0, "unchanged": 0, "edited": 0}
    by_channel: dict[str, int] = {}
    for msg in sorted(messages, key=lambda m: (m.channel_id, m.message_id)):
        res = store_message(conn, msg)
        counts[res.status] += 1
        by_channel[msg.channel_id] = max(by_channel.get(msg.channel_id, 0), msg.message_id)
    for channel_id, top in by_channel.items():
        advance_cursor(conn, channel_id, top, report)
    return counts


def last_ingested_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(ingested_at) AS t FROM source_posts").fetchone()
    return row["t"] if row else None
