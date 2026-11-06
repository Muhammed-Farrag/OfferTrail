"""Acceptance 7–9 and 13: routing, grounded drafts, approval invalidation, manual flow."""

import pytest
from conftest import job_by_key

from jobradar.apply import manual
from jobradar.apply.approval import ApprovalError, approve
from jobradar.apply.drafts import current_draft, edit_draft, prepare_email, validate_proposal
from jobradar.apply.routing import classify_emails, determine_route
from jobradar.apply.state import get_application
from jobradar.llm.fake import FakeLLM
from jobradar.pipeline import paste_description

EMAIL_JOB = "linkedin:9000000001"
FORM_JOB = "linkedin:9000000002"


# 7 ---------------------------------------------------------------------------
def test_accommodation_email_rejected_as_recipient():
    text = ("If you need an accommodation or accessibility support during the application "
            "process, please email accessibility@micro1.example.")
    [cand] = classify_emails(text)
    assert not cand.accepted and "accommodation" in cand.reason
    route = determine_route("https://www.linkedin.com/jobs/view/4487329670/", text)
    assert route.kind == "linkedin" and route.target != "accessibility@micro1.example"


def test_explicit_apply_by_email_is_accepted_with_evidence(loaded):
    app = get_application(loaded.conn, job_by_key(loaded, EMAIL_JOB)["id"])
    assert app["route"] == "email"
    assert app["route_target"] == "careers@nilevision.example"
    assert "To apply, send your CV to careers@nilevision.example" in app["route_evidence"]


def test_generic_company_address_is_not_harvested():
    route = determine_route(None, "Contact us at info@acme.example for anything. We build AI.")
    assert route.kind == "unknown" and route.target is None


def test_email_and_form_together_require_a_choice():
    text = ("To apply, send your CV to jobs@acme.example.\n"
            "Or apply online via our application form: https://acme.example/apply")
    route = determine_route(None, text)
    assert route.kind == "choice"
    assert {o["kind"] for o in route.options} == {"email", "external_form"}


def test_required_subject_is_preserved(loaded):
    preview = prepare_email(loaded, job_by_key(loaded, EMAIL_JOB)["id"])
    assert preview.subject == "Junior MLE - Ref ML-24"


# 8 ---------------------------------------------------------------------------
def test_draft_uses_only_approved_facts_and_selected_cv(loaded):
    conn = loaded.conn
    rejected = conn.execute("SELECT id FROM candidate_facts WHERE text LIKE '%FastAPI%'"
                            ).fetchone()["id"]
    conn.execute("UPDATE candidate_facts SET approved = 0 WHERE id = ?", (rejected,))
    preview = prepare_email(loaded, job_by_key(loaded, EMAIL_JOB)["id"])
    assert "FastAPI" not in preview.body
    d = conn.execute("SELECT * FROM drafts WHERE id = ?", (preview.draft_id,)).fetchone()
    cv = conn.execute("SELECT * FROM candidate_documents WHERE kind = 'cv' AND approved_at IS NOT "
                      "NULL").fetchone()
    assert d["attachment_doc_id"] == cv["id"] and d["attachment_sha256"] == cv["sha256"]
    assert rejected not in eval(d["used_fact_ids_json"])  # noqa: S307 - our own JSON list


def test_llm_claiming_unapproved_skill_falls_back_to_template(loaded):
    loaded.llm = FakeLLM({"draft_email": lambda data: {
        "body": "I have 5 years of Kubernetes and LangChain experience.", "used_fact_ids": []}})
    preview = prepare_email(loaded, job_by_key(loaded, EMAIL_JOB)["id"])
    assert "Kubernetes" not in preview.body and "LangChain" not in preview.body
    assert "rejected" in preview.generator


def test_missing_qualifications_remain_missing(loaded):
    from jobradar.rag.answer import explain_job

    ans = explain_job(loaded, job_by_key(loaded, FORM_JOB)["id"], missing_only=True)
    assert "LangChain" in ans.text  # required by the job, absent from approved facts


def test_validate_proposal_rejects_links_and_unknown_fact_ids(loaded):
    facts = loaded.conn.execute("SELECT id, text FROM candidate_facts").fetchall()
    text = "\n".join(f["text"] for f in facts)
    assert validate_proposal({"body": "See https://evil.example", "used_fact_ids": []}, facts,
                             text)
    assert validate_proposal({"body": "Hello", "used_fact_ids": [9999]}, facts, text)
    assert validate_proposal({"body": "I know Python.", "used_fact_ids": []}, facts, text) is None


# 9 ---------------------------------------------------------------------------
def _ready(loaded):
    job_id = job_by_key(loaded, EMAIL_JOB)["id"]
    preview = prepare_email(loaded, job_id)
    return job_id, preview


@pytest.mark.parametrize("field,value", [("recipient", "other@nilevision.example"),
                                         ("subject", "Different subject"),
                                         ("body", "Edited body text.")])
def test_material_edit_invalidates_approval(loaded, field, value):
    job_id, preview = _ready(loaded)
    approval_id = approve(loaded.conn, preview.draft_id, preview.content_hash[:16], 1)
    new = edit_draft(loaded, preview.draft_id, **{field: value})
    assert new.content_hash != preview.content_hash
    ap = loaded.conn.execute("SELECT invalidated_at FROM approvals WHERE id = ?",
                             (approval_id,)).fetchone()
    assert ap["invalidated_at"] is not None
    with pytest.raises(ApprovalError):  # the old preview's button no longer works
        approve(loaded.conn, preview.draft_id, preview.content_hash[:16], 1)
    with pytest.raises(ApprovalError):  # new content needs its own approval with its own hash
        approve(loaded.conn, new.draft_id, preview.content_hash[:16], 1)
    assert approve(loaded.conn, new.draft_id, new.content_hash[:16], 1)


def test_attachment_change_invalidates_approval(loaded):
    job_id, preview = _ready(loaded)
    approve(loaded.conn, preview.draft_id, preview.content_hash[:16], 1)
    d = current_draft(loaded.conn, get_application(loaded.conn, job_id)["id"])
    path = loaded.conn.execute("SELECT stored_path FROM candidate_documents WHERE id = ?",
                               (d["attachment_doc_id"],)).fetchone()[0]
    with open(path, "a") as f:
        f.write("\ntampered")
    from jobradar.apply.approval import valid_approval

    ap_id = loaded.conn.execute("SELECT id FROM approvals ORDER BY id DESC").fetchone()[0]
    with pytest.raises(ApprovalError, match="attachment"):
        valid_approval(loaded.conn, ap_id)


def test_changed_source_instructions_invalidate_approval(loaded):
    job_id, preview = _ready(loaded)
    ap_id = approve(loaded.conn, preview.draft_id, preview.content_hash[:16], 1)
    paste_description(loaded, job_id, "SYNTHETIC updated listing. Junior ML engineer in Cairo. "
                      "To apply, send your CV to newinbox@nilevision.example.")
    ap = loaded.conn.execute("SELECT * FROM approvals WHERE id = ?", (ap_id,)).fetchone()
    assert ap["invalidated_at"] is not None
    app = get_application(loaded.conn, job_id)
    assert app["state"] == "needs_review"
    assert app["route_target"] == "newinbox@nilevision.example"


def test_approval_requires_matching_hash_prefix(loaded):
    _, preview = _ready(loaded)
    with pytest.raises(ApprovalError):
        approve(loaded.conn, preview.draft_id, "0000000000000000", 1)


# 13 --------------------------------------------------------------------------
def test_opening_form_does_not_mark_applied_manual_confirmation_does(loaded):
    job_id = job_by_key(loaded, FORM_JOB)["id"]
    manual.record_link_opened(loaded.conn, job_id)
    app = get_application(loaded.conn, job_id)
    assert app["state"] == "manual_action" and app["opened_link_at"]
    manual.mark_applied_manually(loaded.conn, job_id, user_id=42)
    app = get_application(loaded.conn, job_id)
    assert app["state"] == "applied_manual"
    assert "self-reported" in app["state_reason"]
    ev = loaded.conn.execute("SELECT actor FROM status_events WHERE application_id = ? "
                             "ORDER BY id DESC", (app["id"],)).fetchone()
    assert ev["actor"] == "user"


def test_later_closure_does_not_overwrite_applied_state(loaded):
    job_id = job_by_key(loaded, FORM_JOB)["id"]
    manual.mark_applied_manually(loaded.conn, job_id, user_id=42)
    paste_description(loaded, job_id, "SYNTHETIC. No longer accepting applications for this "
                      "junior GenAI role.")
    job = loaded.conn.execute("SELECT availability FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job["availability"] == "closed"
    assert get_application(loaded.conn, job_id)["state"] == "applied_manual"
