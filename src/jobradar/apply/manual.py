"""Manual application workflow (LinkedIn / employer forms) and user decisions."""

from __future__ import annotations

import sqlite3

from jobradar.apply.state import APPLIED, InvalidTransition, get_application, transition
from jobradar.db.conn import transaction
from jobradar.jobs import get_job
from jobradar.pipeline import mark_card_dirty
from jobradar.util import iso, loads

LINKEDIN_STEPS = [
    "Open the LinkedIn listing (you must be signed in to LinkedIn).",
    "Check that the listing still accepts applications and that you meet the location terms.",
    "Use Easy Apply or the employer link shown on the listing (availability unknown to the bot).",
    "Attach your approved CV and use the prepared summary if a text field asks for one.",
    "Come back and tap “I applied manually” — opening the link is not recorded as applying.",
]
FORM_STEPS = [
    "Open the employer's application form.",
    "Fill it in with your approved CV and the prepared answers.",
    "Submit it yourself, then tap “I applied manually”.",
]


def manual_instructions(conn: sqlite3.Connection, job_id: int) -> dict:
    app = get_application(conn, job_id)
    if app is None:
        raise InvalidTransition("no application record for this job")
    job = get_job(conn, job_id)
    facts = conn.execute("SELECT id, text FROM candidate_facts WHERE approved = 1 ORDER BY id "
                         "LIMIT 3").fetchall()
    summary = None
    if facts:
        summary = "\n".join(f"• {f['text']}" for f in facts)
    steps = FORM_STEPS if app["route"] == "external_form" else LINKEDIN_STEPS
    return {"url": app["route_target"] or job["destination_url"], "route": app["route"],
            "steps": steps, "prepared_summary": summary,
            "summary_fact_ids": [f["id"] for f in facts], "closed": job["availability"] == "closed"}


def record_link_opened(conn: sqlite3.Connection, job_id: int) -> None:
    """Opening the application link is logged, but the job stays pending manual completion."""
    with transaction(conn, immediate=True):
        app = get_application(conn, job_id)
        if app is None:
            return
        conn.execute("UPDATE applications SET opened_link_at = ? WHERE id = ?", (iso(), app["id"]))
        if app["state"] == "needs_review" and app["route"] in ("linkedin", "external_form"):
            transition(conn, app["id"], "manual_action", "user", "link opened; awaiting "
                       "manual completion")


def mark_applied_manually(conn: sqlite3.Connection, job_id: int, user_id: int) -> None:
    with transaction(conn, immediate=True):
        app = get_application(conn, job_id)
        if app is None:
            raise InvalidTransition("no application record for this job")
        if app["state"] in APPLIED:
            return  # idempotent
        if app["state"] in ("sending", "send_unknown"):
            raise InvalidTransition("an email send is in progress or uncertain; resolve it first")
        transition(conn, app["id"], "applied_manual", "user",
                   f"self-reported by user {user_id} at {iso()} (not externally verified)")
        mark_card_dirty(conn, job_id)


def skip(conn: sqlite3.Connection, job_id: int, reason: str = "skipped by user") -> None:
    with transaction(conn, immediate=True):
        app = get_application(conn, job_id)
        if app is None or app["state"] == "skipped":
            return
        transition(conn, app["id"], "skipped", "user", reason)
        mark_card_dirty(conn, job_id)


def choose_route(conn: sqlite3.Connection, job_id: int, kind: str) -> None:
    """Pick email or form when the instructions offer both. Never applies through both."""
    with transaction(conn, immediate=True):
        app = get_application(conn, job_id)
        if app is None or app["route"] != "choice":
            raise InvalidTransition("this job does not offer a route choice")
        option = next((o for o in loads(app["route_options_json"], []) if o["kind"] == kind), None)
        if option is None:
            raise InvalidTransition(f"{kind} is not one of the offered routes")
        conn.execute("UPDATE applications SET route = ?, route_target = ?, route_evidence = ?, "
                     "updated_at = ? WHERE id = ?",
                     (kind, option["target"], option["evidence"], iso(), app["id"]))
        if kind == "external_form":
            transition(conn, app["id"], "manual_action", "user", "chose the employer form")
        else:
            transition(conn, app["id"], "needs_review", "user",
                       "chose email — tap Prepare email to build a preview")
        mark_card_dirty(conn, job_id)
