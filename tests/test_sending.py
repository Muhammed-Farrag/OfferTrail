"""Acceptance 10–12: claim-based sending, uncertain outcomes, card-update failure."""

import asyncio
import email
import threading

from conftest import job_by_key

from jobradar.apply.approval import approve
from jobradar.apply.drafts import prepare_email
from jobradar.apply.gmail import FakeGmail
from jobradar.apply.sender import recover_stale_claims, resolve_uncertain, send_approved
from jobradar.apply.state import get_application
from jobradar.bot.outbox import flush
from jobradar.db.conn import connect

EMAIL_JOB = "linkedin:9000000001"
SENDER = "demo.candidate@example.com"


def _approved(ctx):
    job_id = job_by_key(ctx, EMAIL_JOB)["id"]
    preview = prepare_email(ctx, job_id)
    return job_id, approve(ctx.conn, preview.draft_id, preview.content_hash[:16], 1)


def _attempts(ctx):
    return ctx.conn.execute("SELECT * FROM send_attempts").fetchall()


# 10 --------------------------------------------------------------------------
def test_replayed_approval_cannot_send_twice(loaded):
    job_id, ap = _approved(loaded)
    gmail = FakeGmail()
    first = send_approved(loaded.conn, ap, gmail, SENDER)
    second = send_approved(loaded.conn, ap, gmail, SENDER)
    assert first.status == "sent" and second.status == "rejected_claim"
    assert len(gmail.accepted) == 1 and len(_attempts(loaded)) == 1


def test_concurrent_callbacks_create_one_attempt(loaded):
    job_id, ap = _approved(loaded)
    db = loaded.settings.db_path
    gmail = FakeGmail()
    results = []
    barrier = threading.Barrier(4)

    def worker():
        conn = connect(db)
        barrier.wait()
        results.append(send_approved(conn, ap, gmail, SENDER).status)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count("sent") == 1
    assert len(gmail.accepted) == 1 and len(_attempts(loaded)) == 1


def test_restart_after_claim_marks_unknown_and_blocks_resend(loaded):
    job_id, ap = _approved(loaded)
    from jobradar.apply.sender import claim

    claim(loaded.conn, ap)  # simulate crash right after claiming, before Gmail returned
    assert recover_stale_claims(loaded.conn) == 1
    assert get_application(loaded.conn, job_id)["state"] == "send_unknown"
    gmail = FakeGmail()
    assert send_approved(loaded.conn, ap, gmail, SENDER).status == "rejected_claim"
    assert gmail.accepted == []


def test_mime_has_exact_recipient_subject_and_attachment(loaded):
    job_id, ap = _approved(loaded)
    gmail = FakeGmail()
    out = send_approved(loaded.conn, ap, gmail, SENDER)
    msg = email.message_from_bytes(gmail.accepted[0])
    assert msg["To"] == "careers@nilevision.example"
    assert msg["Subject"] == "Junior MLE - Ref ML-24"
    names = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert names == ["CV_Demo_Candidate.md"]
    attempt = _attempts(loaded)[0]
    assert attempt["gmail_message_id"] == out.gmail_message_id
    assert get_application(loaded.conn, job_id)["state"] == "applied_gmail"


# 11 --------------------------------------------------------------------------
def test_timeout_after_acceptance_is_send_unknown_without_retry(loaded):
    job_id, ap = _approved(loaded)
    gmail = FakeGmail("timeout_after_accept")
    out = send_approved(loaded.conn, ap, gmail, SENDER)
    assert out.status == "send_unknown"
    app = get_application(loaded.conn, job_id)
    assert app["state"] == "send_unknown"  # not ✅
    gmail.mode = "ok"
    assert send_approved(loaded.conn, ap, gmail, SENDER).status == "rejected_claim"
    assert len(gmail.accepted) == 1  # no blind retry


def test_uncertain_send_resolved_by_user(loaded):
    job_id, ap = _approved(loaded)
    send_approved(loaded.conn, ap, FakeGmail("timeout_after_accept"), SENDER)
    app = get_application(loaded.conn, job_id)
    assert resolve_uncertain(loaded.conn, app["id"], was_sent=True, user_id=1) == "applied_manual"
    assert "Sent mail" in get_application(loaded.conn, job_id)["state_reason"]


def test_confirmed_failure_is_distinct_and_retryable_after_new_approval(loaded):
    job_id, ap = _approved(loaded)
    out = send_approved(loaded.conn, ap, FakeGmail("reject"), SENDER)
    assert out.status == "failed"
    assert get_application(loaded.conn, job_id)["state"] == "send_failed"
    preview = prepare_email(loaded, job_id)
    ap2 = approve(loaded.conn, preview.draft_id, preview.content_hash[:16], 1)
    assert send_approved(loaded.conn, ap2, FakeGmail(), SENDER).status == "sent"


def test_paused_blocks_sending(loaded):
    _, ap = _approved(loaded)
    gmail = FakeGmail()
    assert send_approved(loaded.conn, ap, gmail, SENDER, paused=True).status == "rejected_claim"
    assert gmail.accepted == []


# 12 --------------------------------------------------------------------------
class FlakyTransport:
    def __init__(self):
        self.fail_edits = False
        self.sent, self.edits = [], []

    async def send(self, chat_id, reply):
        self.sent.append(reply)
        return len(self.sent)

    async def edit(self, chat_id, message_id, reply):
        if self.fail_edits:
            raise RuntimeError("Telegram API unavailable")
        self.edits.append((message_id, reply))


def test_gmail_success_then_card_failure_keeps_state_and_retries_only_card(loaded):
    t = FlakyTransport()
    asyncio.run(flush(loaded, t, 1, limit=50))
    job_id, ap = _approved(loaded)
    gmail = FakeGmail()
    send_approved(loaded.conn, ap, gmail, SENDER)
    t.fail_edits = True
    counts = asyncio.run(flush(loaded, t, 1, limit=50))
    assert counts["failed"] >= 1
    assert get_application(loaded.conn, job_id)["state"] == "applied_gmail"
    # retry later: card updates, Gmail is not called again
    loaded.conn.execute("UPDATE notifications SET next_attempt_at = '2000-01-01T00:00:00+00:00' "
                        "WHERE state = 'pending'")
    t.fail_edits = False
    counts = asyncio.run(flush(loaded, t, 1, limit=50))
    assert counts["edited"] >= 1
    assert any("Applied via Gmail" in r.text for _, r in t.edits)
    assert len(gmail.accepted) == 1
