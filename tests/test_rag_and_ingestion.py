"""Acceptance 15–16: grounded answers; crash-safe ingestion and coverage reporting."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import FIXTURES, NOW, job_by_key, match_of

from jobradar.candidate import import_document
from jobradar.ingest.fixtures import load_fixture
from jobradar.ingest.store import RawMessage, get_cursor, ingest_batch, store_message
from jobradar.ingest.telegram_reader import collect
from jobradar.llm.fake import FakeLLM
from jobradar.pipeline import process_pending_posts
from jobradar.rag.answer import answer


# 15 --------------------------------------------------------------------------
def test_explanations_cite_stored_fact_chunks(loaded):
    job_id = job_by_key(loaded, "linkedin:9000000001")["id"]
    ans = answer(loaded, f"Why does #{job_id} match me?")
    assert ans.kind == "explanation" and ans.citations
    stored = {r[0] for r in loaded.conn.execute("SELECT id FROM chunks WHERE collection = "
                                                "'candidate'")}
    assert set(ans.citations) <= stored


def test_counts_and_status_answers_come_from_database(loaded):
    ans = answer(loaded, "Which applications need manual work?")
    n = loaded.conn.execute("SELECT COUNT(*) FROM applications WHERE state = 'manual_action'"
                            ).fetchone()[0]
    assert ans.kind == "sql" and len(ans.rows) == n
    jobs = answer(loaded, "Show junior AI jobs from this week")
    assert jobs.kind == "sql"
    assert all(r["seniority"] in ("junior", "intern") for r in jobs.rows)
    assert {r["id"] for r in jobs.rows} >= {job_by_key(loaded, "linkedin:9000000001")["id"]}


def test_missing_evidence_yields_explicit_unknown(ctx):
    ans = answer(ctx, "What did I study at university?")  # nothing indexed yet
    assert ans.kind == "unknown" and "Unknown" in ans.text


def test_llm_citations_outside_retrieved_set_are_rejected(loaded):
    loaded.llm = FakeLLM({"grounded_answer": lambda d: {
        "answer": "You have a PhD.", "citations": ["cand:f999"], "unknowns": []}})
    ans = answer(loaded, "Tell me about my PyTorch image classification model")
    assert loaded.llm.calls, "evidence must have been retrieved and passed to the model"
    assert "PhD" not in ans.text and ans.kind == "unknown"


def test_llm_unavailable_returns_evidence_not_fabrication(loaded):
    loaded.llm = FakeLLM(unavailable=True)
    ans = answer(loaded, "Tell me about my PyTorch image classification model")
    assert "LLM unavailable" in ans.text and ans.citations


def test_unreadable_cv_is_flagged_not_silently_empty(ctx, tmp_path):
    bad = tmp_path / "scan.pdf"
    bad.write_bytes(b"%PDF-1.4 not really a pdf")
    res = import_document(ctx.conn, ctx.settings.documents_dir, bad, "cv")
    assert res.status != "ok" and res.fact_count == 0
    from jobradar.candidate import profile_status

    assert profile_status(ctx.conn)["extraction_problems"]


def test_filtering_works_before_onboarding_but_no_personal_claims(ctx):
    from conftest import ingest_fixture

    ingest_fixture(ctx)
    m = match_of(ctx, job_by_key(ctx, "linkedin:9000000004")["id"])
    assert m["decision"] == "needs_review"
    assert "no approved candidate facts" in m["explanation_json"]


# 16 --------------------------------------------------------------------------
def test_crash_between_store_and_cursor_loses_nothing(ctx):
    messages, _ = load_fixture(FIXTURES / "channel_sample.json")
    for m in messages[:5]:
        store_message(ctx.conn, m)  # stored, then "crash" before the cursor advanced
    assert get_cursor(ctx.conn, messages[0].channel_id) is None
    counts = ingest_batch(ctx.conn, messages)  # restart re-delivers everything
    assert counts["unchanged"] == 5 and counts["new"] == len(messages) - 5
    assert ctx.conn.execute("SELECT COUNT(*) FROM source_posts").fetchone()[0] == len(messages)
    assert get_cursor(ctx.conn, messages[0].channel_id) == max(m.message_id for m in messages)


def test_processing_failure_is_retried_independently(ctx, monkeypatch):
    messages, _ = load_fixture(FIXTURES / "channel_sample.json")
    ingest_batch(ctx.conn, messages)
    import jobradar.pipeline as pl

    real = pl.upsert_job_from_alert
    boom = {"n": 0}

    def flaky(conn, alert, post):
        if post["message_id"] == 76593 and boom["n"] == 0:
            boom["n"] += 1
            raise RuntimeError("transient")
        return real(conn, alert, post)

    monkeypatch.setattr(pl, "upsert_job_from_alert", flaky)
    first = process_pending_posts(ctx)
    assert first["errors"] == 1 and first["jobs_new"] >= 10
    second = process_pending_posts(ctx)
    assert second["errors"] == 0
    assert job_by_key(ctx, "linkedin:4487329670") is not None


def test_edit_creates_new_version_and_invalidates_analysis(loaded):
    job = job_by_key(loaded, "linkedin:9000000004")
    before = match_of(loaded, job["id"])["id"]
    post = loaded.conn.execute("SELECT * FROM source_posts WHERE message_id = 99004").fetchone()
    edited = RawMessage(post["channel_id"], 99004, post["raw_text"].replace(
        "LLM Engineer", "Junior LLM Engineer"), NOW, eval(post["links_json"]),  # noqa: S307
        edited_at=NOW)
    assert store_message(loaded.conn, edited).status == "edited"
    process_pending_posts(loaded)
    after = match_of(loaded, job["id"])
    assert after["id"] != before and after["seniority"] == "junior"
    versions = loaded.conn.execute("SELECT COUNT(*) FROM source_posts WHERE message_id = 99004"
                                   ).fetchone()[0]
    assert versions == 2


def _fake_iter(ids, start=NOW):
    msgs = [SimpleNamespace(id=i, date=start - timedelta(hours=(max(ids) - i))) for i in ids]

    def iter_messages(limit=None, min_id=0):
        async def gen():
            n = 0
            for m in sorted(msgs, key=lambda m: -m.id):
                if m.id <= (min_id or 0) or (limit is not None and n >= limit):
                    return
                n += 1
                yield m
        return gen()
    return iter_messages


def test_backfill_respects_day_and_message_limits():
    it = _fake_iter(list(range(1, 400)))  # one per hour → ~16 days
    got, report = asyncio.run(collect(it, None, max_messages=500, since_days=7))
    assert report["hit_day_limit"] and report["complete"]
    assert len(got) == 7 * 24 + 1
    got, report = asyncio.run(collect(it, None, max_messages=50, since_days=7))
    assert len(got) == 50 and not report["complete"]


def test_catch_up_reports_incomplete_coverage():
    it = _fake_iter(list(range(1, 1001)))
    got, report = asyncio.run(collect(it, cursor=100, max_messages=500, since_days=7))
    assert len(got) == 500 and report["complete"] is False
    assert "101–500" in report["gap"]
    got, report = asyncio.run(collect(it, cursor=900, max_messages=500, since_days=7))
    assert len(got) == 100 and report["complete"] is True


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_posts_are_unparseable_not_jobs(ctx, text):
    ingest_batch(ctx.conn, [RawMessage("c", 1, text, NOW)])
    counts = process_pending_posts(ctx)
    assert counts["unparseable"] == 1 and counts["jobs_new"] == 0
