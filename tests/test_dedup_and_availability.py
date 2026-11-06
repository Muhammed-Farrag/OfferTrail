"""Acceptance 5–6: deduplication, closures vs. fetch failures."""

import httpx
from conftest import job_by_key, match_of

from jobradar.apply.drafts import DraftError, prepare_email
from jobradar.apply.state import get_application
from jobradar.jobs import job_source_posts, possible_duplicates_of
from jobradar.parse.linkedin import linkedin_job_id
from jobradar.pipeline import fetch_description


# 5 ---------------------------------------------------------------------------
def test_linkedin_id_canonicalization():
    variants = [
        "https://ae.linkedin.com/jobs/view/ai-engineer-remote-at-joblet-ai-4487329670",
        "https://www.linkedin.com/jobs/view/4487329670/?trk=public_jobs_topcard&refId=x",
        "https://eg.linkedin.com/jobs/view/ai-engineer-remote-at-joblet-ai-4487329670?position=1",
        "https://www.linkedin.com/jobs/search/?currentJobId=4487329670&keywords=ai",
    ]
    assert {linkedin_job_id(u) for u in variants} == {"4487329670"}
    assert linkedin_job_id("https://evil.example/jobs/view/4487329670") is None


def test_repost_variants_share_one_job_with_two_sources(loaded):
    job = job_by_key(loaded, "linkedin:4487329670")
    posts = job_source_posts(loaded.conn, job["id"])
    assert sorted(p["message_id"] for p in posts) == [76593, 76605]
    assert loaded.conn.execute("SELECT COUNT(*) FROM jobs WHERE linkedin_id = '4487329670'"
                               ).fetchone()[0] == 1


def test_same_title_different_ids_are_possible_duplicates_not_merged(loaded):
    a = job_by_key(loaded, "linkedin:4487100005")
    b = job_by_key(loaded, "linkedin:4487100006")
    assert a is not None and b is not None and a["id"] != b["id"]
    assert possible_duplicates_of(loaded.conn, a["id"]) == [b["id"]]


# 6 ---------------------------------------------------------------------------
def test_explicit_closure_is_recorded_and_blocks_send_flow(loaded):
    job = job_by_key(loaded, "linkedin:4487329670")
    assert job["availability"] == "closed"
    assert "No longer accepting applications" in job["availability_evidence"]
    assert job["availability_observed_at"]
    app = get_application(loaded.conn, job["id"])
    assert app["route"] != "email"
    try:
        prepare_email(loaded, job["id"])
    except DraftError:
        pass
    else:
        raise AssertionError("closed listing must not enter the send flow")


def test_fetch_failure_is_unknown_not_closed(loaded):
    loaded.settings.fetch_enabled = True
    job = job_by_key(loaded, "linkedin:9000000004")  # no description stored

    def handler(request):
        return httpx.Response(403, text="authwall")

    loaded.fetch_transport = httpx.MockTransport(handler)
    import jobradar.enrich.safe_fetch as sf

    original = sf._check_host
    sf._check_host = lambda host, resolver=None: None  # skip DNS (no network in tests)
    try:
        res = fetch_description(loaded, job["id"])
    finally:
        sf._check_host = original
    assert res["status"] == "blocked"
    job = job_by_key(loaded, "linkedin:9000000004")
    assert job["availability"] == "unknown"
    assert match_of(loaded, job["id"])["decision"] == "needs_review"


def test_ssrf_guard_blocks_private_addresses():
    from jobradar.enrich.safe_fetch import BlockedURL, validate_url

    def resolver(host, port):
        return [(None, None, None, None, ("127.0.0.1", 0))]

    for url in ("http://localhost/x", "file:///etc/passwd", "http://user:pw@example.com/"):
        try:
            validate_url(url, resolver)
        except BlockedURL:
            continue
        raise AssertionError(f"{url} should be blocked")
