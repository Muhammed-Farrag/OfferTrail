"""Durable, claim-based sending.

1. In one IMMEDIATE transaction: re-validate the approval, insert a `claimed` send attempt
   (a partial UNIQUE index allows only one live attempt per application) and move the
   application to `sending`. A replayed or concurrent callback fails here and sends nothing.
2. Call Gmail outside the transaction.
3. Record `sent` (+ Gmail message ID), `failed` (confirmed rejection) or `send_unknown`.

Exactly-once delivery across an external API cannot be guaranteed; what is guaranteed is at
most one *automatic* attempt per approval, and no automatic retry after an uncertain outcome.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from jobradar.apply.approval import ApprovalError, valid_approval
from jobradar.apply.gmail import GmailRejected, GmailSender, GmailUncertain, build_mime
from jobradar.apply.state import InvalidTransition, transition
from jobradar.db.conn import transaction
from jobradar.pipeline import mark_card_dirty
from jobradar.util import iso

log = logging.getLogger(__name__)


@dataclass
class SendOutcome:
    status: str  # sent | failed | send_unknown | rejected_claim
    detail: str
    gmail_message_id: str | None = None
    attempt_id: int | None = None


def claim(conn: sqlite3.Connection, approval_id: int) -> tuple[int, sqlite3.Row]:
    """Atomically claim the right to send. Raises ApprovalError/InvalidTransition/IntegrityError."""
    with transaction(conn, immediate=True):
        ap, draft = valid_approval(conn, approval_id)
        app_id = draft["application_id"]
        transition(conn, app_id, "sending", "user", "approved send in progress",
                   expected_from={"ready_for_approval"})
        attempt_id = conn.execute(
            "INSERT INTO send_attempts (application_id, draft_id, approval_id, content_hash, "
            "state, claimed_at) VALUES (?, ?, ?, ?, 'claimed', ?)",
            (app_id, draft["id"], ap["id"], draft["content_hash"], iso())).lastrowid
        return attempt_id, draft


def send_approved(conn: sqlite3.Connection, approval_id: int, gmail: GmailSender,
                  sender_address: str, paused: bool = False) -> SendOutcome:
    if paused:
        return SendOutcome("rejected_claim", "sending is paused (/resume to continue)")
    try:
        attempt_id, draft = claim(conn, approval_id)
    except (ApprovalError, InvalidTransition) as e:
        return SendOutcome("rejected_claim", str(e))
    except sqlite3.IntegrityError:
        return SendOutcome("rejected_claim", "a send attempt already exists for this application")

    app_id = draft["application_id"]
    job_id = conn.execute("SELECT job_id FROM applications WHERE id = ?", (app_id,)).fetchone()[0]
    cv_path = Path(conn.execute("SELECT stored_path FROM candidate_documents WHERE id = ?",
                                (draft["attachment_doc_id"],)).fetchone()[0])
    try:
        mime = build_mime(sender=sender_address, recipient=draft["recipient"],
                          subject=draft["subject"], body=draft["body"], attachment_path=cv_path,
                          attachment_filename=draft["attachment_filename"])
    except OSError as e:  # nothing left the machine
        return _finish(conn, attempt_id, app_id, job_id, "failed", "send_failed",
                       f"could not build message: {e}")
    try:
        message_id = gmail.send(mime)
    except GmailRejected as e:
        return _finish(conn, attempt_id, app_id, job_id, "failed", "send_failed", str(e))
    except GmailUncertain as e:
        return _finish(conn, attempt_id, app_id, job_id, "send_unknown", "send_unknown",
                       f"{e}. Check Gmail Sent mail before trying again.")
    return _finish(conn, attempt_id, app_id, job_id, "sent", "applied_gmail",
                   "Gmail accepted the message for sending", message_id)


def _finish(conn, attempt_id, app_id, job_id, attempt_state, app_state, detail,
            message_id=None) -> SendOutcome:
    with transaction(conn, immediate=True):
        conn.execute("UPDATE send_attempts SET state = ?, gmail_message_id = ?, error = ?, "
                     "finished_at = ? WHERE id = ?",
                     (attempt_state, message_id, None if attempt_state == "sent" else detail,
                      iso(), attempt_id))
        transition(conn, app_id, app_state, "gmail" if attempt_state != "failed" else "system",
                   detail if message_id is None else f"{detail} (Gmail id {message_id})")
        # The card update is a separate, retryable notification — a failure there can't
        # undo the recorded send.
        mark_card_dirty(conn, job_id)
    return SendOutcome("sent" if attempt_state == "sent" else attempt_state, detail, message_id,
                       attempt_id)


def recover_stale_claims(conn: sqlite3.Connection) -> int:
    """On startup: a `claimed` attempt without a result means we crashed mid-send."""
    rows = conn.execute("SELECT id, application_id FROM send_attempts WHERE state = 'claimed'"
                        ).fetchall()
    for r in rows:
        job_id = conn.execute("SELECT job_id FROM applications WHERE id = ?",
                              (r["application_id"],)).fetchone()[0]
        _finish(conn, r["id"], r["application_id"], job_id, "send_unknown", "send_unknown",
                "process stopped during sending; check Gmail Sent mail before trying again")
    return len(rows)


def resolve_uncertain(conn: sqlite3.Connection, application_id: int, was_sent: bool,
                      user_id: int) -> str:
    """User checked Sent mail. Sent → applied (self-verified); not sent → failed (retryable)."""
    with transaction(conn, immediate=True):
        att = conn.execute("SELECT id FROM send_attempts WHERE application_id = ? AND "
                           "state = 'send_unknown'", (application_id,)).fetchone()
        if att is None:
            raise InvalidTransition("no uncertain send for this application")
        job_id = conn.execute("SELECT job_id FROM applications WHERE id = ?",
                              (application_id,)).fetchone()[0]
        if was_sent:
            conn.execute("UPDATE send_attempts SET state = 'sent', error = 'confirmed by user in "
                         "Sent mail', finished_at = ? WHERE id = ?", (iso(), att["id"]))
            transition(conn, application_id, "applied_manual", "user",
                       f"user {user_id} confirmed the email is in Gmail Sent mail")
            result = "applied_manual"
        else:
            conn.execute("UPDATE send_attempts SET state = 'failed', error = 'user found no "
                         "message in Sent mail', finished_at = ? WHERE id = ?", (iso(), att["id"]))
            transition(conn, application_id, "send_failed", "user",
                       "not found in Sent mail; prepare and approve again to retry")
            result = "send_failed"
        mark_card_dirty(conn, job_id)
    return result
