"""Approval binding.

An approval is tied to the content hash of one exact draft (recipient, subject, body,
attachment bytes, job identity and the source application instructions). The approve button
carries a prefix of that hash, so approving an older preview after an edit fails. Only an
authorized user action creates approvals — never the model.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jobradar.apply.drafts import draft_content_hash
from jobradar.db.conn import transaction
from jobradar.jobs import get_job, latest_enrichment
from jobradar.util import iso, sha256_bytes

HASH_PREFIX_LEN = 16


class ApprovalError(Exception):
    pass


def check_draft_still_valid(conn: sqlite3.Connection, draft: sqlite3.Row) -> None:
    if draft["superseded_at"]:
        raise ApprovalError("this preview was replaced by a newer draft")
    recomputed = draft_content_hash(
        draft["recipient"], draft["subject"], draft["body"], draft["attachment_sha256"],
        draft["attachment_filename"], _job_id(conn, draft), draft["instruction_hash"])
    if recomputed != draft["content_hash"]:
        raise ApprovalError("draft content does not match its hash")
    app = conn.execute("SELECT * FROM applications WHERE id = ?",
                       (draft["application_id"],)).fetchone()
    job = get_job(conn, app["job_id"])
    if job["availability"] == "closed":
        raise ApprovalError("listing is closed")
    enr = latest_enrichment(conn, job["id"])
    if enr is None or enr["instruction_hash"] != draft["instruction_hash"]:
        raise ApprovalError("the job's application instructions changed since this draft")
    cv = conn.execute("SELECT stored_path, sha256 FROM candidate_documents WHERE id = ?",
                      (draft["attachment_doc_id"],)).fetchone()
    p = Path(cv["stored_path"])
    if not p.exists() or sha256_bytes(p.read_bytes()) != draft["attachment_sha256"]:
        raise ApprovalError("attachment file is missing or changed")


def _job_id(conn, draft) -> int:
    return conn.execute("SELECT job_id FROM applications WHERE id = ?",
                        (draft["application_id"],)).fetchone()[0]


def approve(conn: sqlite3.Connection, draft_id: int, presented_hash_prefix: str,
            approved_by: int) -> int:
    """Create (or return the existing) approval for exactly this draft."""
    with transaction(conn, immediate=True):
        draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if draft is None:
            raise ApprovalError("draft not found")
        if len(presented_hash_prefix) < 8 or \
                not draft["content_hash"].startswith(presented_hash_prefix):
            raise ApprovalError("approval does not match the current preview")
        app = conn.execute("SELECT state FROM applications WHERE id = ?",
                           (draft["application_id"],)).fetchone()
        if app["state"] != "ready_for_approval":
            raise ApprovalError(f"application is {app['state']}, not ready for approval")
        check_draft_still_valid(conn, draft)
        existing = conn.execute(
            "SELECT id FROM approvals WHERE draft_id = ? AND content_hash = ? AND "
            "invalidated_at IS NULL", (draft_id, draft["content_hash"])).fetchone()
        if existing:
            return existing["id"]
        return conn.execute(
            "INSERT INTO approvals (draft_id, content_hash, approved_by, approved_at) "
            "VALUES (?, ?, ?, ?)", (draft_id, draft["content_hash"], approved_by, iso())
        ).lastrowid


def valid_approval(conn: sqlite3.Connection, approval_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
    """(approval, draft) if the approval still binds the current draft; raises otherwise."""
    ap = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if ap is None or ap["invalidated_at"]:
        raise ApprovalError("approval was invalidated" if ap else "approval not found")
    draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (ap["draft_id"],)).fetchone()
    if draft["content_hash"] != ap["content_hash"]:
        raise ApprovalError("approval does not match draft content")
    check_draft_still_valid(conn, draft)
    return ap, draft
