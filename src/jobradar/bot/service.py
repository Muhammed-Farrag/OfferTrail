"""Bot logic independent of the Telegram library (the PTB adapter in bot/app.py is thin).

Every entry point first checks that the update comes from the configured user in the
configured private chat. Every button re-validates the current stored state server-side, so
stale, replayed or forged callbacks cannot act on old state.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field

from jobradar.apply import manual
from jobradar.apply.approval import ApprovalError, approve
from jobradar.apply.drafts import DraftError, current_draft, edit_draft, prepare_email
from jobradar.apply.gmail import GmailSender
from jobradar.apply.sender import resolve_uncertain, send_approved
from jobradar.apply.state import DISPLAY, InvalidTransition, get_application
from jobradar.bot.cards import Button, Reply, e, render_job_card, render_preview
from jobradar.bot.outbox import is_paused, set_paused
from jobradar.candidate import approve_cv, list_facts, profile_status, review_facts
from jobradar.config import Preferences, save_prefs
from jobradar.context import AppContext
from jobradar.health import format_health, health
from jobradar.pipeline import mark_card_dirty, paste_description, reanalyze_all
from jobradar.rag.answer import answer, applications_in, list_jobs

log = logging.getLogger(__name__)

HELP = """<b>AI Job Radar</b> — commands
/jobs [week|all|uncertain] — qualifying jobs, best first
/job &lt;id&gt; — show one job card
/pending — applications that need you
/applied — applications recorded as applied
/profile — CV and approved facts (send a PDF/DOCX/TXT to import a CV)
/approvefacts — approve all extracted facts · /rejectfact &lt;id&gt;
/approvecv — approve the newest CV version for email applications
/settings — preferences · /set &lt;key&gt; &lt;value&gt;
/editdraft &lt;job id&gt; subject|body|to &lt;text&gt; — edit an email draft (needs re-approval)
/pause · /resume — stop/start new notifications and sending
/health — ingestion, backlog, credentials, model errors, uncertain sends
Ask in plain text, e.g. “Show junior AI jobs from this week”, “Why does #12 match me?”,
“What is missing from my CV for #12?”, “Which applications need manual work?”"""


@dataclass
class Result:
    replies: list[Reply] = field(default_factory=list)
    toast: str | None = None          # short callback answer
    refresh_job: int | None = None    # re-render this job's card in place


class BotService:
    def __init__(self, ctx: AppContext, gmail_factory=None) -> None:
        self.ctx = ctx
        self.gmail_factory = gmail_factory  # () -> GmailSender, created lazily
        self._gmail: GmailSender | None = None

    # ------------------------------------------------------------------ auth
    def authorized(self, user_id: int | None, chat_id: int | None) -> bool:
        s = self.ctx.settings
        if s.bot_allowed_user_id is None or user_id is None:
            return False
        if user_id != s.bot_allowed_user_id:
            return False
        allowed_chat = s.bot_allowed_chat_id if s.bot_allowed_chat_id is not None else user_id
        return chat_id == allowed_chat

    def unauthorized_reply(self, user_id: int | None) -> Reply:
        if self.ctx.settings.bot_allowed_user_id is None:
            return Reply(f"Setup mode: this bot is not bound to anyone yet. Your Telegram user ID "
                         f"is <code>{user_id}</code>. Put it in JOBRADAR_BOT_ALLOWED_USER_ID and "
                         f"restart.")
        return Reply("This is a private bot.")

    # ------------------------------------------------------------------ commands
    def command(self, user_id: int, chat_id: int, name: str, args: list[str]) -> Result:
        if not self.authorized(user_id, chat_id):
            return Result([self.unauthorized_reply(user_id)])
        handler = getattr(self, f"cmd_{name}", None)
        if handler is None:
            return Result([Reply("Unknown command. /help")])
        try:
            return handler(args)
        except (ValueError, KeyError) as ex:
            return Result([Reply(f"Error: {e(ex)}")])

    def cmd_start(self, args) -> Result:
        p = profile_status(self.ctx.conn)
        missing = self.ctx.prefs.missing()
        lines = ["<b>AI Job Radar</b> is watching your source channel.",
                 f"Profile: {p['facts_approved']} approved facts, CV "
                 f"{'approved' if p['ready_for_email'] else 'not approved'}."]
        if missing:
            lines.append("Not set yet: " + ", ".join(missing) + " (/settings)")
        lines.append("Filtering works now; personalized explanations and email applications "
                     "need onboarding (/profile).")
        return Result([Reply("\n".join(lines)), Reply(HELP)])

    def cmd_help(self, args) -> Result:
        return Result([Reply(HELP)])

    def cmd_jobs(self, args) -> Result:
        mode = args[0] if args else "week"
        if mode == "uncertain":
            rows = list_jobs(self.ctx, decisions=("uncertain",), limit=20)
        else:
            rows = list_jobs(self.ctx, days=None if mode == "all" else 7, limit=15)
        if not rows:
            return Result([Reply("No qualifying jobs yet.")])
        lines = [f"<b>Jobs ({e(mode)})</b>"]
        for r in rows:
            st = DISPLAY.get(r["state"], "") if r["state"] else ""
            closed = " ⛔" if r["availability"] == "closed" else ""
            lines.append(f"#{r['id']} {e(r['title'])} — {e(r['publisher'])} · "
                         f"{r['rank_score']}{closed} {st}  /job {r['id']}")
        return Result([Reply("\n".join(lines))])

    def cmd_job(self, args) -> Result:
        job_id = int(args[0].lstrip("#"))
        self._set_focus(job_id)
        return Result([render_job_card(self.ctx.conn, job_id, self._profile_ready())])

    def cmd_pending(self, args) -> Result:
        rows = applications_in(self.ctx, ("needs_review", "ready_for_approval", "manual_action",
                                          "send_unknown", "send_failed"))
        if not rows:
            return Result([Reply("Nothing pending.")])
        return Result([Reply("<b>Pending</b>\n" + "\n".join(
            f"#{r['job_id']} {e(r['title'])} — {DISPLAY[r['state']]}: {e(r['state_reason'])}"
            for r in rows))])

    def cmd_applied(self, args) -> Result:
        rows = applications_in(self.ctx, ("applied_gmail", "applied_manual"))
        if not rows:
            return Result([Reply("No applications recorded as applied.")])
        return Result([Reply("<b>Applied</b>\n" + "\n".join(
            f"#{r['job_id']} {e(r['title'])} — {DISPLAY[r['state']]} ({r['updated_at'][:16]}): "
            f"{e(r['state_reason'])}" for r in rows))])

    def cmd_profile(self, args) -> Result:
        p = profile_status(self.ctx.conn)
        facts = list_facts(self.ctx.conn)
        lines = ["<b>Profile</b>",
                 f"Approved CV: {e(p['approved_cv']['filename']) + ' v' + str(p['approved_cv']['version']) if p['approved_cv'] else 'none'}",  # noqa: E501
                 f"Facts: {p['facts_approved']}/{p['facts_total']} approved"]
        for prob in p["extraction_problems"]:
            lines.append(f"⚠️ {e(prob['filename'])}: {e(prob['extraction_status'])} — send a text "
                         f"version (TXT/MD) instead")
        for f in facts[:40]:
            mark = "✅" if f["approved"] else "▫️"
            lines.append(f"{mark} [{f['id']}] {e(f['text'])}")
        lines.append("Only approved facts are used in explanations and emails.")
        return Result([Reply("\n".join(lines), [[Button("Approve all facts", "facts:all")]])])

    def cmd_approvefacts(self, args) -> Result:
        return self._approve_all_facts()

    def cmd_rejectfact(self, args) -> Result:
        review_facts(self.ctx.conn, approve=[], reject=[int(a) for a in args])
        self._reindex()
        return Result([Reply(f"Rejected facts {', '.join(args)}.")])

    def cmd_approvecv(self, args) -> Result:
        row = self.ctx.conn.execute("SELECT id, filename, version, extraction_status FROM "
                                    "candidate_documents WHERE kind = 'cv' ORDER BY version DESC "
                                    "LIMIT 1").fetchone()
        if row is None:
            return Result([Reply("No CV imported yet — send the file to this chat.")])
        approve_cv(self.ctx.conn, row["id"])
        return Result([Reply(f"Approved {e(row['filename'])} v{row['version']} as the CV "
                             f"attached to email applications.")])

    def cmd_settings(self, args) -> Result:
        prefs = self.ctx.prefs.model_dump()
        lines = ["<b>Settings</b>"] + [
            f"{k}: {e(v) if v is not None else '⚠️ not set'}" for k, v in prefs.items()]
        lines.append("Change with /set key value (lists: comma-separated; booleans: true/false)")
        return Result([Reply("\n".join(lines))])

    def cmd_set(self, args) -> Result:
        if len(args) < 2:
            raise ValueError("usage: /set key value")
        key, raw = args[0], " ".join(args[1:])
        if key not in Preferences.model_fields:
            raise ValueError(f"unknown setting {key}")
        data = self.ctx.prefs.model_dump()
        current = data.get(key)
        if raw.lower() in ("none", "null", "unset"):
            value = None
        elif isinstance(current, bool) or key.startswith("include_") or key in (
                "require_sponsorship_abroad", "open_to_relocation"):
            value = raw.lower() in ("true", "yes", "1", "on")
        elif key in ("max_years_experience",):
            value = int(raw)
        elif key in ("citizenship", "languages", "work_modes", "target_families"):
            value = [x.strip() for x in raw.split(",") if x.strip()]
        else:
            value = raw
        data[key] = value
        prefs = Preferences.model_validate(data)
        save_prefs(self.ctx.settings.prefs_path, prefs)
        self.ctx.prefs = prefs
        n = reanalyze_all(self.ctx)
        return Result([Reply(f"Set {e(key)} = {e(value)}. Re-analyzed {n} jobs.")])

    def cmd_editdraft(self, args) -> Result:
        if len(args) < 3 or args[1] not in ("subject", "body", "to"):
            raise ValueError("usage: /editdraft <job id> subject|body|to <text>")
        job_id = int(args[0].lstrip("#"))
        app = get_application(self.ctx.conn, job_id)
        d = current_draft(self.ctx.conn, app["id"]) if app else None
        if d is None:
            raise ValueError("no draft for this job")
        text = " ".join(args[2:])
        field_name = {"to": "recipient"}.get(args[1], args[1])
        try:
            edit_draft(self.ctx, d["id"], **{field_name: text})
        except DraftError as ex:
            return Result([Reply(f"Cannot edit: {e(ex)}")])
        return Result([render_preview(self.ctx.conn, job_id)], refresh_job=job_id)

    def cmd_pause(self, args) -> Result:
        set_paused(self.ctx, True)
        return Result([Reply("Paused: no new job notifications and no email sending. Ingestion "
                             "keeps storing posts so nothing is missed. /resume to continue.")])

    def cmd_resume(self, args) -> Result:
        set_paused(self.ctx, False)
        return Result([Reply("Resumed notifications and sending.")])

    def cmd_health(self, args) -> Result:
        return Result([Reply(format_health(health(self.ctx)))])

    # ------------------------------------------------------------------ free text
    def text(self, user_id: int, chat_id: int, text: str) -> Result:
        if not self.authorized(user_id, chat_id):
            return Result([self.unauthorized_reply(user_id)])
        awaiting = self._kv("awaiting_paste")
        if awaiting:
            self._kv_set("awaiting_paste", None)
            res = paste_description(self.ctx, int(awaiting), text)
            if res["status"] != "ok":
                return Result([Reply(f"Not stored: {e(res.get('error'))}")])
            return Result([Reply(f"Stored description v{res['enrichment_version']} for "
                                 f"#{awaiting}." + ("\n" + "\n".join(res["notes"])
                                                    if res["notes"] else ""))],
                          refresh_job=int(awaiting))
        focus = self._kv("focus_job")
        ans = answer(self.ctx, text, int(focus) if focus else None)
        body = html.escape(ans.text)
        if ans.unknowns and ans.kind == "rag":
            body += "\nUnknown: " + html.escape("; ".join(ans.unknowns))
        return Result([Reply(body)])

    # ------------------------------------------------------------------ callbacks
    def callback(self, user_id: int, chat_id: int, data: str) -> Result:
        if not self.authorized(user_id, chat_id):
            return Result(toast="Not authorized")
        parts = data.split(":")
        action = parts[0]
        try:
            handler = getattr(self, f"cb_{action}")
        except AttributeError:
            return Result(toast="Unknown action")
        try:
            return handler(*parts[1:], user_id=user_id)
        except (ApprovalError, DraftError, InvalidTransition, ValueError, KeyError) as ex:
            return Result([Reply(f"Not done: {e(ex)}")], toast="Not done",
                          refresh_job=self._job_from_parts(action, parts))

    def _job_from_parts(self, action, parts) -> int | None:
        if action == "appr":
            row = self.ctx.conn.execute("SELECT a.job_id FROM drafts d JOIN applications a ON "
                                        "a.id = d.application_id WHERE d.id = ?",
                                        (int(parts[1]),)).fetchone()
            return row[0] if row else None
        try:
            return int(parts[1])
        except (IndexError, ValueError):
            return None

    def cb_why(self, job_id, user_id) -> Result:
        self._set_focus(int(job_id))
        ans = answer(self.ctx, f"why does #{job_id} match me")
        return Result([Reply(html.escape(ans.text))])

    def cb_open(self, job_id, user_id) -> Result:
        job_id = int(job_id)
        info = manual.manual_instructions(self.ctx.conn, job_id)
        if info["closed"]:
            return Result([Reply("The listing is closed; no application link offered.")])
        manual.record_link_opened(self.ctx.conn, job_id)
        lines = ["<b>Manual application</b> — opening the link does not mark you as applied."]
        lines += [f"{i}. {e(s)}" for i, s in enumerate(info["steps"], 1)]
        if info["prepared_summary"]:
            lines += ["", "Prepared summary (from your approved facts "
                      f"{info['summary_fact_ids']}):", e(info["prepared_summary"])]
        buttons = [[Button("Open application", url=info["url"])],
                   [Button("I applied manually", f"manual:{job_id}"),
                    Button("Skip", f"skip:{job_id}")]]
        return Result([Reply("\n".join(lines), buttons)], refresh_job=job_id)

    def cb_paste(self, job_id, user_id) -> Result:
        self._kv_set("awaiting_paste", str(int(job_id)))
        return Result([Reply(f"Paste the full job description for #{job_id} as your next "
                             f"message. It is stored as untrusted text and never executed.")])

    def cb_prep(self, job_id, user_id) -> Result:
        job_id = int(job_id)
        preview = prepare_email(self.ctx, job_id)
        mark_card_dirty(self.ctx.conn, job_id)
        return Result([render_preview(self.ctx.conn, job_id)], refresh_job=job_id,
                      toast=f"Draft {preview.draft_id} ready")

    def cb_prev(self, job_id, user_id) -> Result:
        return Result([render_preview(self.ctx.conn, int(job_id))])

    def cb_appr(self, draft_id, hash_prefix, user_id) -> Result:
        if is_paused(self.ctx):
            return Result([Reply("Sending is paused. /resume first.")], toast="Paused")
        if not self.ctx.settings.gmail_sender or self.gmail_factory is None:
            return Result([Reply("Gmail is not configured (run `jobradar auth-gmail` and set "
                                 "JOBRADAR_GMAIL_SENDER). Nothing was sent.")], toast="No Gmail")
        approval_id = approve(self.ctx.conn, int(draft_id), hash_prefix, user_id)
        if self._gmail is None:
            self._gmail = self.gmail_factory()
        out = send_approved(self.ctx.conn, approval_id, self._gmail, self.ctx.settings.gmail_sender)
        job = self._job_from_parts("appr", ["appr", draft_id])
        msg = {"sent": f"✅ Gmail accepted the message (id {out.gmail_message_id}). This confirms "
                       "sending, not delivery or review.",
               "failed": f"❌ Send failed: {out.detail}",
               "send_unknown": f"⚠️ Outcome uncertain: {out.detail}",
               "rejected_claim": f"Not sent: {out.detail}"}[out.status]
        return Result([Reply(e(msg))], toast=out.status, refresh_job=job)

    def cb_manual(self, job_id, user_id) -> Result:
        manual.mark_applied_manually(self.ctx.conn, int(job_id), user_id)
        return Result(toast="Recorded as applied manually (self-reported)",
                      refresh_job=int(job_id))

    def cb_skip(self, job_id, user_id) -> Result:
        manual.skip(self.ctx.conn, int(job_id))
        return Result(toast="Skipped", refresh_job=int(job_id))

    def cb_route(self, job_id, kind, user_id) -> Result:
        manual.choose_route(self.ctx.conn, int(job_id), kind)
        return Result(toast=f"Route: {kind}", refresh_job=int(job_id))

    def cb_sent(self, job_id, answer_flag, user_id) -> Result:
        app = get_application(self.ctx.conn, int(job_id))
        state = resolve_uncertain(self.ctx.conn, app["id"], answer_flag == "y", user_id)
        return Result(toast=DISPLAY[state], refresh_job=int(job_id))

    def cb_facts(self, which, user_id) -> Result:
        return self._approve_all_facts()

    # ------------------------------------------------------------------ helpers
    def _approve_all_facts(self) -> Result:
        ids = [f["id"] for f in list_facts(self.ctx.conn) if not f["approved"]]
        review_facts(self.ctx.conn, approve=ids)
        n = self._reindex()
        reanalyze_all(self.ctx)
        return Result([Reply(f"Approved {len(ids)} facts; indexed {n} candidate chunks.")])

    def _reindex(self) -> int:
        from jobradar.candidate import index_candidate

        return index_candidate(self.ctx.conn, self.ctx.embedder) if self.ctx.embedder else 0

    def _profile_ready(self) -> bool:
        return profile_status(self.ctx.conn)["ready_for_email"]

    def _kv(self, key: str) -> str | None:
        row = self.ctx.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _kv_set(self, key: str, value: str | None) -> None:
        if value is None:
            self.ctx.conn.execute("DELETE FROM kv WHERE key = ?", (key,))
        else:
            self.ctx.conn.execute("INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                                  "value = excluded.value", (key, value))

    def _set_focus(self, job_id: int) -> None:
        self._kv_set("focus_job", str(job_id))
