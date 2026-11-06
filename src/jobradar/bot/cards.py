"""Job cards and previews rendered as Telegram HTML. All source text is escaped.

Availability, match decision, application route and application state are separate lines.
Buttons appear only when the action is valid for the current stored state.
"""

from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass, field

from jobradar.apply.approval import HASH_PREFIX_LEN
from jobradar.apply.drafts import current_draft
from jobradar.apply.state import APPLIED, DISPLAY, get_application
from jobradar.jobs import get_job, job_source_posts, latest_enrichment, possible_duplicates_of
from jobradar.util import loads, sha256_text

FAMILY_LABEL = {"ai_ml": "AI/ML engineering", "genai": "GenAI/LLM engineering",
                "research_eng": "Research engineering", "data_science": "Data science",
                "evaluation": "AI evaluation/annotation", "software_generic": "Generic software",
                "unrelated": "Unrelated"}
ROUTE_LABEL = {"email": "Email (explicit instructions)", "linkedin": "LinkedIn (manual)",
               "external_form": "Employer form (manual)", "unknown": "Unknown",
               "choice": "Email or form — your choice"}


@dataclass
class Button:
    label: str
    callback: str | None = None
    url: str | None = None


@dataclass
class Reply:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)

    def fingerprint(self) -> str:
        return sha256_text(self.text, repr([[(b.label, b.callback, b.url) for b in row]
                                            for row in self.buttons]))


def e(s) -> str:
    return html.escape(str(s)) if s is not None else "unknown"


def render_job_card(conn: sqlite3.Connection, job_id: int, profile_ready: bool = True) -> Reply:
    job = get_job(conn, job_id)
    m = conn.execute("SELECT * FROM match_results WHERE job_id = ? AND invalidated_at IS NULL",
                     (job_id,)).fetchone()
    app = get_application(conn, job_id)
    enr = latest_enrichment(conn, job_id)
    posts = job_source_posts(conn, job_id)
    unknowns = loads(m["unknowns_json"], []) if m else []
    reasons = loads(m["reasons_json"], []) if m else []
    closed = job["availability"] == "closed"

    lines = [f"<b>{e(job['title'])}</b> — {e(job['publisher'])}  <i>#{job_id}</i>"]
    loc = e(job["location"])
    if job["work_mode"] != "unknown":
        loc += f" · {job['work_mode']}"
    loc_unknowns = [u for u in unknowns if "eligib" in u or "sponsor" in u or "country" in u]
    if loc_unknowns:
        loc += "; " + "; ".join(e(u) for u in loc_unknowns)
    lines.append(f"Location: {loc}")
    if m:
        lines.append(f"Seniority: {e(m['seniority'])} · {FAMILY_LABEL.get(m['role_family'], '?')}")
    avail = {"open": "open when checked", "closed": "closed when checked",
             "unknown": "unknown (not checked or inaccessible)"}[job["availability"]]
    if job["availability_observed_at"]:
        avail += f" ({job['availability_observed_at'][:10]})"
    lines.append(f"Availability: {avail}")
    if m:
        score = f" · rank {m['rank_score']}" if m["rank_score"] is not None else ""
        lines.append(f"Match: {e(m['decision'])}{score} · evidence {e(m['evidence_confidence'])}")
        other = [u for u in unknowns if u not in loc_unknowns]
        if other:
            lines.append("Unknown: " + "; ".join(e(u) for u in other))
        exp = loads(m["explanation_json"], None)
        if exp and exp["strengths"]:
            lines.append("Strengths: " + ", ".join(e(s["requirement"]) for s in exp["strengths"]))
        if exp and exp["missing"]:
            lines.append("Missing: " + ", ".join(e(x) for x in exp["missing"]))
        if m["decision"] in ("excluded", "ignored", "uncertain") and reasons:
            lines.append("Reason: " + "; ".join(e(r) for r in reasons))
    flags = loads(job["flags_json"], [])
    if "publisher_employer_differs" in flags:
        lines.append(f"⚠️ Listing publisher ({e(job['publisher'])}) differs from employer named "
                     f"in the description ({e(job['stated_employer'])}) — not verified.")
    dups = possible_duplicates_of(conn, job_id)
    if dups:
        lines.append("Possible duplicate of: " + ", ".join(f"#{d}" for d in dups))
    if app:
        lines.append(f"Route: {ROUTE_LABEL[app['route']]}")
        if app["route"] == "email" and app["route_target"]:
            lines.append(f"Recipient: {e(app['route_target'])} — “{e(app['route_evidence'])}”")
    if closed and (app is None or app["state"] not in APPLIED):
        ev = job["availability_evidence"] or "closure reported"
        lines.append(f"⛔ Closed — {e(ev)}")
    elif app:
        lines.append(f"Status: {DISPLAY[app['state']]}" +
                     (f" — {e(app['state_reason'])}" if app["state_reason"] else ""))
    links = []
    if posts and posts[-1]["permalink"]:
        links.append(f'<a href="{e(posts[-1]["permalink"])}">Telegram post</a>')
    if job["destination_url"]:
        links.append(f'<a href="{e(job["destination_url"])}">Listing</a>')
    if len(posts) > 1:
        links.append(f"{len(posts)} source posts")
    if links:
        lines.append("Source: " + " · ".join(links))

    return Reply("\n".join(lines), _buttons(conn, job, m, app, enr, posts, profile_ready))


def _buttons(conn, job, m, app, enr, posts, profile_ready) -> list[list[Button]]:
    jid = job["id"]
    closed = job["availability"] == "closed"
    rows: list[list[Button]] = []
    row1 = []
    if m and m["decision"] in ("candidate", "needs_review"):
        row1.append(Button("Why this matches", f"why:{jid}"))
    if posts and posts[-1]["permalink"]:
        row1.append(Button("Open source", url=posts[-1]["permalink"]))
    if row1:
        rows.append(row1)
    if app is None:
        return rows
    state = app["state"]
    row2 = []
    if not closed and state in ("needs_review", "manual_action") and \
            app["route"] in ("linkedin", "external_form", "unknown") and \
            (app["route_target"] or job["destination_url"]):
        row2.append(Button("Open application", f"open:{jid}"))
    if enr is None:
        row2.append(Button("Paste description", f"paste:{jid}"))
    if row2:
        rows.append(row2)
    row3 = []
    if app["route"] == "choice" and state == "needs_review" and not closed:
        row3 += [Button("Use email", f"route:{jid}:email"),
                 Button("Use form", f"route:{jid}:external_form")]
    if app["route"] == "email" and state in ("needs_review", "send_failed") and not closed \
            and profile_ready:
        row3.append(Button("Prepare email", f"prep:{jid}"))
    if state == "ready_for_approval" and not closed:
        d = current_draft(conn, app["id"])
        if d:
            row3.append(Button("Preview email", f"prev:{jid}"))
            row3.append(Button("Approve & send",
                               f"appr:{d['id']}:{d['content_hash'][:HASH_PREFIX_LEN]}"))
    if state == "send_unknown":
        row3 += [Button("✅ It is in Sent mail", f"sent:{jid}:y"),
                 Button("❌ Not in Sent mail", f"sent:{jid}:n")]
    if row3:
        rows.append(row3)
    row4 = []
    if state in ("needs_review", "manual_action", "send_failed"):
        row4.append(Button("I applied manually", f"manual:{jid}"))
    if state not in APPLIED and state not in ("skipped", "sending", "send_unknown"):
        row4.append(Button("Skip", f"skip:{jid}"))
    if row4:
        rows.append(row4)
    return rows


def render_preview(conn: sqlite3.Connection, job_id: int) -> Reply:
    app = get_application(conn, job_id)
    d = current_draft(conn, app["id"]) if app else None
    if d is None:
        return Reply("No email draft for this job.")
    cv = conn.execute("SELECT version FROM candidate_documents WHERE id = ?",
                      (d["attachment_doc_id"],)).fetchone()
    text = "\n".join([
        f"<b>Email preview</b> for #{job_id} (draft {d['id']}, hash {d['content_hash'][:12]})",
        f"To: {e(d['recipient'])}",
        f"Recipient evidence: “{e(d['recipient_evidence'])}”",
        f"Subject: {e(d['subject'])}",
        f"Attachment: {e(d['attachment_filename'])} (CV v{cv['version']}, "
        f"sha256 {d['attachment_sha256'][:12]})",
        f"Generated by: {e(d['generator'])}",
        "",
        f"<pre>{e(d['body'])}</pre>",
        "",
        "Approving sends exactly this message. Any edit requires a new approval.",
    ])
    buttons = []
    if app["state"] == "ready_for_approval":
        buttons.append([Button("Approve & send",
                               f"appr:{d['id']}:{d['content_hash'][:HASH_PREFIX_LEN]}"),
                        Button("Skip", f"skip:{job_id}")])
    return Reply(text, buttons)
