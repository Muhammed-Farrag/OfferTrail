"""Acceptance 1–4: filtering, seniority, eligibility, opt-in categories."""

from conftest import egypt_prefs, ingest_fixture, job_by_key, match_of

from jobradar.match.rules import JobInput, decide
from jobradar.parse.alert_parser import parse_alert


def _kind_of(ctx, message_id):
    return ctx.conn.execute("SELECT kind FROM source_posts WHERE message_id = ?",
                            (message_id,)).fetchone()["kind"]


# 1 ---------------------------------------------------------------------------
def test_batch_announcement_and_waiter_ignored_driver_not_ai(ctx):
    ingest_fixture(ctx)
    assert _kind_of(ctx, 76574) == "announcement"
    waiter = job_by_key(ctx, "linkedin:4487100002")
    assert match_of(ctx, waiter["id"])["decision"] == "ignored"
    driver = job_by_key(ctx, "linkedin:4487100001")
    m = match_of(ctx, driver["id"])
    assert m["decision"] == "ignored" and m["role_family"] == "unrelated"


def test_announcement_creates_no_job(ctx):
    ingest_fixture(ctx)
    assert ctx.conn.execute("SELECT COUNT(*) FROM jobs WHERE canonical_key LIKE 'post:%'"
                            ).fetchone()[0] == 0


# 2 ---------------------------------------------------------------------------
def test_senior_python_excluded_under_junior_settings(ctx):
    ingest_fixture(ctx)
    m = match_of(ctx, job_by_key(ctx, "linkedin:4487100003")["id"])
    assert m["decision"] == "excluded"
    assert m["seniority"] == "senior"
    assert m["role_family"] == "software_generic"  # "AI" in the company name is irrelevant


def test_unlabelled_ai_engineer_stays_seniority_unknown(ctx):
    ingest_fixture(ctx)
    m = match_of(ctx, job_by_key(ctx, "linkedin:4487329670")["id"])
    assert m["role_family"] == "ai_ml"
    assert m["seniority"] == "unknown"
    assert m["decision"] == "needs_review"
    assert "seniority not stated" in m["unknowns_json"]


# 3 ---------------------------------------------------------------------------
def test_uae_remote_does_not_pass_egypt_rule_and_is_visibly_unresolved(ctx):
    ingest_fixture(ctx)
    m = match_of(ctx, job_by_key(ctx, "linkedin:4487329670")["id"])
    assert m["eligibility"] == "unknown"
    assert "cross-border remote eligibility unknown" in m["unknowns_json"]


def test_remote_restricted_elsewhere_is_excluded():
    job = JobInput("AI Engineer (Remote)", "United Arab Emirates", "remote",
                   remote_scope={"status": "restricted", "allowed": ["AE"]})
    d = decide(job, egypt_prefs())
    assert d.decision == "excluded" and d.eligibility == "excluded"


def test_remote_open_to_emea_is_eligible_for_egypt():
    job = JobInput("Junior AI Engineer (Remote)", "United Arab Emirates", "remote",
                   remote_scope={"status": "open", "allowed": ["emea"]})
    d = decide(job, egypt_prefs())
    assert d.eligibility == "eligible" and d.decision == "candidate"


def test_abroad_onsite_requires_sponsorship():
    base = dict(title="Junior ML Engineer", location="Berlin, Germany", work_mode="onsite")
    unknown = decide(JobInput(**base), egypt_prefs())
    assert unknown.decision == "needs_review"
    assert "⚠️ visa sponsorship unknown" in unknown.unknowns
    assert decide(JobInput(**base, sponsorship="no"), egypt_prefs()).decision == "excluded"
    yes = decide(JobInput(**base, sponsorship="yes"), egypt_prefs())
    assert yes.decision == "candidate" and yes.sponsorship_confirmed


def test_egypt_onsite_needs_no_sponsorship():
    d = decide(JobInput("Junior ML Engineer", "Cairo, Egypt", "onsite"), egypt_prefs())
    assert d.eligibility == "eligible" and d.decision == "candidate"


def test_missing_residence_setting_is_visible():
    d = decide(JobInput("Junior ML Engineer", "Cairo, Egypt", "onsite"),
               egypt_prefs(residence_country=None))
    assert d.decision == "needs_review"
    assert any("residence country is not set" in u for u in d.unknowns)


# 4 ---------------------------------------------------------------------------
def test_ai_evaluator_separate_unless_enabled(ctx):
    ingest_fixture(ctx)
    m = match_of(ctx, job_by_key(ctx, "linkedin:4487100004")["id"])
    assert m["role_family"] == "evaluation" and m["decision"] == "excluded"
    enabled = decide(JobInput("Arabic AI Evaluator (Remote)", "Egypt", "remote"),
                     egypt_prefs(include_evaluation_gigs=True))
    assert enabled.role_family == "evaluation" and enabled.decision in ("candidate",
                                                                        "needs_review")


def test_parser_extracts_fields_and_keeps_relative_age_as_text():
    a = parse_alert("AI Engineer (Remote)\n🏢 Joblet-AI\n📍 United Arab Emirates\n"
                    "🕒 4 minutes ago\n📌 via LinkedIn\n\n➡️ Apply Here",
                    [{"text": "➡️ Apply Here",
                      "url": "https://ae.linkedin.com/jobs/view/ai-engineer-remote-at-joblet-ai-"
                             "4487329670"}])
    assert a.kind == "job"
    assert (a.title, a.company, a.location) == ("AI Engineer (Remote)", "Joblet-AI",
                                                "United Arab Emirates")
    assert a.reported_age == "4 minutes ago" and a.work_mode == "remote"
    assert a.linkedin_id == "4487329670"


def test_missing_fields_stay_unknown():
    a = parse_alert("Machine Learning Engineer\n\n➡️ Apply Here",
                    [{"text": "➡️ Apply Here", "url": "https://www.linkedin.com/jobs/view/1234567"}])
    assert a.kind == "job" and a.company is None and a.location is None
    assert a.work_mode == "unknown"
