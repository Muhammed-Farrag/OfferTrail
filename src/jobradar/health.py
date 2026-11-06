"""Actionable health report."""

from __future__ import annotations

from jobradar.bot.outbox import is_paused
from jobradar.candidate import profile_status
from jobradar.context import AppContext
from jobradar.llm.budget import usage_today
from jobradar.rag.store import stale_model_count
from jobradar.util import loads


def health(ctx: AppContext) -> dict:
    c, s = ctx.conn, ctx.settings
    cursor = c.execute("SELECT * FROM ingest_cursors ORDER BY updated_at DESC LIMIT 1").fetchone()
    report = {
        "paused": is_paused(ctx),
        "last_ingestion": c.execute("SELECT MAX(ingested_at) FROM source_posts").fetchone()[0],
        "cursor": dict(cursor) | {"last_run": loads(cursor["last_run_json"], None)}
        if cursor else None,
        "unprocessed_posts": c.execute(
            "SELECT COUNT(*) FROM source_posts WHERE is_current = 1 AND processed_at IS NULL"
        ).fetchone()[0],
        "failed_posts": c.execute(
            "SELECT COUNT(*) FROM source_posts WHERE process_error IS NOT NULL AND "
            "processed_at IS NULL").fetchone()[0],
        "pending_notifications": c.execute(
            "SELECT COUNT(*) FROM notifications WHERE state = 'pending'").fetchone()[0],
        "failed_notifications": c.execute(
            "SELECT COUNT(*) FROM notifications WHERE state = 'failed'").fetchone()[0],
        "uncertain_sends": [dict(r) for r in c.execute(
            "SELECT a.job_id, j.title, s.error FROM send_attempts s JOIN applications a ON "
            "a.id = s.application_id JOIN jobs j ON j.id = a.job_id WHERE s.state = "
            "'send_unknown'")],
        "llm": {"backend": s.llm_backend, "configured": ctx.llm is not None,
                "model": s.llm_model, "budget_tokens": s.llm_daily_token_budget,
                "today": usage_today(c)},
        "embeddings": {"backend": s.embed_backend,
                       "model": ctx.embedder.model_id if ctx.embedder else None,
                       "stale_vectors": stale_model_count(c, ctx.embedder) if ctx.embedder else 0},
        "credentials": {
            "telegram_reader": bool(s.telegram_api_id and s.telegram_api_hash),
            "telegram_session": s.telegram_session_path.with_suffix(".session").exists(),
            "bot_token": bool(s.bot_token),
            "bot_allowed_user": s.bot_allowed_user_id is not None,
            "gmail_token": s.gmail_token_path.exists(),
            "gmail_sender": bool(s.gmail_sender),
        },
        "profile": {k: v for k, v in profile_status(c).items() if k != "approved_cv"},
        "missing_preferences": ctx.prefs.missing(),
    }
    return report


def format_health(h: dict) -> str:
    lines = ["<b>Health</b>"]
    lines.append(f"Paused: {'yes — notifications and sending are paused' if h['paused'] else 'no'}")
    lines.append(f"Last ingestion: {h['last_ingestion'] or 'never'}")
    if h["cursor"]:
        lr = h["cursor"].get("last_run") or {}
        cov = "complete" if lr.get("complete", True) else f"INCOMPLETE — {lr.get('gap', '?')}"
        lines.append(f"Cursor: message {h['cursor']['last_message_id']} · coverage {cov}")
    lines.append(f"Backlog: {h['unprocessed_posts']} unprocessed posts "
                 f"({h['failed_posts']} with errors)")
    lines.append(f"Notifications: {h['pending_notifications']} pending, "
                 f"{h['failed_notifications']} failed")
    if h["uncertain_sends"]:
        lines.append("⚠️ Uncertain sends — check Gmail Sent mail: " + ", ".join(
            f"#{u['job_id']} {u['title']}" for u in h["uncertain_sends"]))
    llm = h["llm"]
    lines.append(f"LLM: {llm['backend']} {'ready' if llm['configured'] else 'NOT configured'} · "
                 f"today {llm['today']['tokens']}/{llm['budget_tokens']} tokens, "
                 f"{llm['today']['errors']} errors"
                 + (f" (last: {llm['today']['last_error']})" if llm["today"]["last_error"] else ""))
    lines.append(f"Embeddings: {h['embeddings']['model']}"
                 + (f" · {h['embeddings']['stale_vectors']} vectors need reindex"
                    if h["embeddings"]["stale_vectors"] else ""))
    missing = [k for k, v in h["credentials"].items() if not v]
    lines.append("Credentials missing: " + (", ".join(missing) if missing else "none"))
    p = h["profile"]
    lines.append(f"Profile: {p['facts_approved']}/{p['facts_total']} facts approved · "
                 f"CV {'approved' if p['ready_for_email'] else 'not approved'}")
    if p["extraction_problems"]:
        lines.append("Extraction problems: " + ", ".join(
            f"{x['filename']} ({x['extraction_status']})" for x in p["extraction_problems"]))
    if h["missing_preferences"]:
        lines.append("Preferences not set: " + ", ".join(h["missing_preferences"]))
    return "\n".join(lines)
