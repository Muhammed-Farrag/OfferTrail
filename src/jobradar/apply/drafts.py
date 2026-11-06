"""Email application drafts built only from approved candidate facts.

The model may *propose* body text, but the recipient, subject and attachment are fixed by
code, and a proposal is rejected (falling back to a template) if it cites unknown facts,
claims skills the approved facts don't contain, or includes addresses/links.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from jobradar.apply.state import get_application, transition
from jobradar.candidate import approved_cv
from jobradar.context import AppContext
from jobradar.db.conn import transaction
from jobradar.jobs import get_job, latest_enrichment
from jobradar.llm.base import LLMUnavailable, strict_object
from jobradar.match.explain import skills_in
from jobradar.rag.store import search
from jobradar.util import dumps, iso, sha256_bytes, sha256_text

DRAFT_SYSTEM = (
    "Write a short, plain-text job application email body (under 180 words). Use ONLY the "
    "approved candidate facts provided; do not add skills, employers, degrees, years or "
    "results that are not in them. Do not include email addresses, links, a subject line or a "
    "signature block beyond the candidate's name. Report the IDs of the facts you used."
)
DRAFT_SCHEMA = strict_object({
    "body": {"type": "string"},
    "used_fact_ids": {"type": "array", "items": {"type": "integer"}},
})
_EMAIL_OR_URL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|https?://|www\.", re.I)


class DraftError(Exception):
    pass


@dataclass
class DraftPreview:
    draft_id: int
    recipient: str
    recipient_evidence: str
    subject: str
    body: str
    attachment_filename: str
    attachment_version: int
    content_hash: str
    generator: str


def draft_content_hash(recipient: str, subject: str, body: str, attachment_sha256: str,
                       attachment_filename: str, job_id: int, instruction_hash: str) -> str:
    return sha256_text(recipient.strip().lower(), subject, body, attachment_sha256,
                       attachment_filename, str(job_id), instruction_hash)


def _select_facts(ctx: AppContext, query: str, limit: int = 4) -> list[sqlite3.Row]:
    conn = ctx.conn
    approved = {r["id"]: r for r in conn.execute(
        "SELECT id, section, text FROM candidate_facts WHERE approved = 1")}
    picked: list[sqlite3.Row] = []
    if ctx.embedder is not None and query:
        for hit in search(conn, ctx.embedder, "candidate", query, k=limit * 2):
            fact = approved.get(hit.source_id)
            if fact and fact not in picked:
                picked.append(fact)
    for fact in approved.values():  # fall back to profile order
        if len(picked) >= limit:
            break
        if fact not in picked:
            picked.append(fact)
    return picked[:limit]


def validate_proposal(out: dict, facts: list[sqlite3.Row], all_approved_text: str) -> str | None:
    """Returns a rejection reason, or None if the proposal is acceptable."""
    body = out.get("body")
    if not isinstance(body, str) or not body.strip():
        return "empty body"
    if len(body) > 3000:
        return "body too long"
    ids = out.get("used_fact_ids") or []
    allowed = {f["id"] for f in facts}
    if any(i not in allowed for i in ids):
        return "cites facts that were not provided"
    if _EMAIL_OR_URL.search(body):
        return "contains an address or link"
    claimed = set(skills_in(body)) - set(skills_in(all_approved_text))
    if claimed:
        return f"claims skills not in approved facts: {', '.join(sorted(claimed))}"
    return None


def _template_body(name: str | None, title: str, employer: str | None,
                   facts: list[sqlite3.Row]) -> str:
    lines = [f"Dear {employer or 'Hiring Team'},", "",
             f"I would like to apply for the {title} position."]
    if facts:
        lines += ["", "A few points from my background:"]
        lines += [f"- {f['text']}" for f in facts[:3]]
    lines += ["", "My CV is attached. Thank you for considering my application.", "",
              "Kind regards,"]
    if name:
        lines.append(name)
    return "\n".join(lines)


def prepare_email(ctx: AppContext, job_id: int) -> DraftPreview:
    conn = ctx.conn
    job = get_job(conn, job_id)
    app = get_application(conn, job_id)
    if app is None or app["route"] != "email" or not app["route_target"]:
        raise DraftError("this job has no evidenced application email")
    if job["availability"] == "closed":
        raise DraftError("listing is closed; email applications are disabled")
    if app["state"] not in ("needs_review", "ready_for_approval", "send_failed"):
        raise DraftError(f"application is {app['state']}; cannot prepare a new email")
    cv = approved_cv(conn)
    if cv is None:
        raise DraftError("no approved CV — complete onboarding (/profile) first")
    cv_path = Path(cv["stored_path"])
    if not cv_path.exists() or sha256_bytes(cv_path.read_bytes()) != cv["sha256"]:
        raise DraftError("approved CV file is missing or changed on disk — re-import it")
    enr = latest_enrichment(conn, job_id)
    if enr is None:
        raise DraftError("no stored job description with application instructions")

    approved_rows = conn.execute("SELECT text FROM candidate_facts WHERE approved = 1").fetchall()
    if not approved_rows:
        raise DraftError("no approved candidate facts")
    all_text = "\n".join(r["text"] for r in approved_rows)
    facts = _select_facts(ctx, enr["description"] or job["title"] or "")
    employer = job["stated_employer"] or job["publisher"]
    name = ctx.prefs.full_name

    body, used, generator = None, [], "template@v1"
    if ctx.llm_allowed():
        ctx.llm_calls_this_cycle += 1
        try:
            out = ctx.llm.complete_json(
                task="draft_email", system=DRAFT_SYSTEM, schema=DRAFT_SCHEMA,
                data={"job_title": job["title"], "employer": employer, "candidate_name": name,
                      "job_requirements": skills_in(enr["description"]),
                      "approved_facts": [{"id": f["id"], "text": f["text"]} for f in facts]})
            problem = validate_proposal(out, facts, all_text)
            if problem is None:
                body, used, generator = out["body"].strip(), out["used_fact_ids"], \
                    f"llm:{ctx.llm.model_id}"
            else:
                generator = f"template@v1 (LLM proposal rejected: {problem})"
        except LLMUnavailable as e:
            generator = f"template@v1 (LLM unavailable: {e})"
    if body is None:
        body = _template_body(name, job["title"] or "advertised", employer, facts)
        used = [f["id"] for f in facts[:3]]

    from jobradar.apply.routing import required_subject

    subject = required_subject(enr["description"]) or \
        f"Application: {job['title']}" + (f" — {name}" if name else "")
    filename = f"CV_{(name or 'candidate').replace(' ', '_')}{cv_path.suffix}"
    return _save_draft(conn, app, job_id, recipient=app["route_target"],
                       evidence=app["route_evidence"] or "", subject=subject, body=body,
                       cv=cv, filename=filename, instruction_hash=enr["instruction_hash"],
                       used=used, generator=generator)


def _save_draft(conn, app, job_id, *, recipient, evidence, subject, body, cv, filename,
                instruction_hash, used, generator) -> DraftPreview:
    content_hash = draft_content_hash(recipient, subject, body, cv["sha256"], filename, job_id,
                                      instruction_hash)
    now = iso()
    with transaction(conn, immediate=True):
        old = conn.execute("SELECT id FROM drafts WHERE application_id = ? AND superseded_at IS "
                           "NULL", (app["id"],)).fetchone()
        if old:
            conn.execute("UPDATE drafts SET superseded_at = ? WHERE id = ?", (now, old["id"]))
            conn.execute("UPDATE approvals SET invalidated_at = ?, invalidation_reason = "
                         "'draft replaced' WHERE draft_id = ? AND invalidated_at IS NULL",
                         (now, old["id"]))
        draft_id = conn.execute(
            "INSERT INTO drafts (application_id, recipient, recipient_evidence, subject, body, "
            "attachment_doc_id, attachment_filename, attachment_sha256, instruction_hash, "
            "used_fact_ids_json, content_hash, generator, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (app["id"], recipient, evidence, subject, body, cv["id"], filename, cv["sha256"],
             instruction_hash, dumps(used), content_hash, generator, now)).lastrowid
        current = conn.execute("SELECT state FROM applications WHERE id = ?",
                               (app["id"],)).fetchone()["state"]
        if current != "ready_for_approval":
            transition(conn, app["id"], "ready_for_approval", "system",
                       "complete email preview exists")
        else:
            transition(conn, app["id"], "ready_for_approval", "system", "draft edited; "
                       "re-approval required")
    return DraftPreview(draft_id, recipient, evidence, subject, body, filename, cv["version"],
                        content_hash, generator)


def edit_draft(ctx: AppContext, draft_id: int, *, recipient: str | None = None,
               subject: str | None = None, body: str | None = None) -> DraftPreview:
    """Any material edit creates a new draft version and voids existing approvals."""
    conn = ctx.conn
    d = conn.execute("SELECT * FROM drafts WHERE id = ? AND superseded_at IS NULL",
                     (draft_id,)).fetchone()
    if d is None:
        raise DraftError("draft not found or already replaced")
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (d["application_id"],)
                       ).fetchone()
    if app["state"] != "ready_for_approval":
        raise DraftError(f"application is {app['state']}; drafts can't be edited now")
    cv = conn.execute("SELECT * FROM candidate_documents WHERE id = ?",
                      (d["attachment_doc_id"],)).fetchone()
    evidence = d["recipient_evidence"]
    if recipient is not None and recipient.strip().lower() != d["recipient"].lower():
        evidence = "recipient entered manually by the user (not from job instructions)"
    return _save_draft(conn, app, app["job_id"], recipient=(recipient or d["recipient"]).strip(),
                       evidence=evidence, subject=subject if subject is not None else d["subject"],
                       body=body if body is not None else d["body"], cv=cv,
                       filename=d["attachment_filename"], instruction_hash=d["instruction_hash"],
                       used=loads_list(d["used_fact_ids_json"]),
                       generator=d["generator"] + "+user_edit")


def loads_list(s: str) -> list:
    import json

    return json.loads(s or "[]")


def current_draft(conn: sqlite3.Connection, application_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM drafts WHERE application_id = ? AND superseded_at IS NULL",
                        (application_id,)).fetchone()
