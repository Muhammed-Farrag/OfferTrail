"""Processing pipeline: stored posts → canonical jobs → analysis → application + card.

Each step is idempotent so it can be re-run after crashes or edits.
"""

from __future__ import annotations

import logging

from jobradar.apply.routing import Route, determine_route
from jobradar.apply.state import APPLIED, IN_FLIGHT, get_application, transition
from jobradar.context import AppContext
from jobradar.db.conn import transaction
from jobradar.enrich import llm_extract
from jobradar.enrich.extract import EXTRACTOR_VERSION, extract_facts, html_to_text
from jobradar.enrich.safe_fetch import fetch
from jobradar.jobs import add_enrichment, get_job, latest_enrichment, upsert_job_from_alert
from jobradar.match.explain import explain
from jobradar.match.rank import evidence_confidence, rank
from jobradar.match.rules import RULES_VERSION, JobInput, decide
from jobradar.parse.alert_parser import parse_alert
from jobradar.rag.chunk import chunk_description
from jobradar.rag.store import add_chunks, delete_collection_source
from jobradar.util import dumps, iso, loads

log = logging.getLogger(__name__)
MAX_PROCESS_ATTEMPTS = 5
NOTIFY_DECISIONS = ("candidate", "needs_review")


# ------------------------------------------------------------------ posts → jobs
def process_pending_posts(ctx: AppContext, limit: int = 200) -> dict:
    conn = ctx.conn
    rows = conn.execute(
        "SELECT * FROM source_posts WHERE is_current = 1 AND processed_at IS NULL AND attempts < ?"
        " ORDER BY message_id LIMIT ?", (MAX_PROCESS_ATTEMPTS, limit)).fetchall()
    counts = {"jobs_new": 0, "jobs_seen": 0, "announcements": 0, "unparseable": 0, "errors": 0}
    for post in rows:
        try:
            alert = parse_alert(post["raw_text"], loads(post["links_json"], []))
            if alert.kind == "job":
                job_id, created = upsert_job_from_alert(conn, alert, post)
                counts["jobs_new" if created else "jobs_seen"] += 1
                analyze_job(ctx, job_id)
            else:
                counts["announcements" if alert.kind == "announcement" else "unparseable"] += 1
            conn.execute("UPDATE source_posts SET kind = ?, processed_at = ?, process_error = NULL"
                         " WHERE id = ?", (alert.kind, iso(), post["id"]))
        except Exception as e:  # recorded and retried; one bad post must not stop the rest
            log.exception("processing post %s failed", post["id"])
            counts["errors"] += 1
            conn.execute("UPDATE source_posts SET attempts = attempts + 1, process_error = ? "
                         "WHERE id = ?", (f"{type(e).__name__}: {e}"[:500], post["id"]))
    return counts


# ------------------------------------------------------------------ enrichment
def index_job_description(ctx: AppContext, job_id: int) -> int:
    enr = latest_enrichment(ctx.conn, job_id)
    if ctx.embedder is None or enr is None:
        return 0
    with transaction(ctx.conn):
        delete_collection_source(ctx.conn, "job", job_id=job_id)
    items = [(f"job:j{job_id}e{enr['version']}:c{i}", enr["id"], enr["version"], text)
             for i, text in enumerate(chunk_description(enr["description"]))]
    return add_chunks(ctx.conn, ctx.embedder, "job", items, job_id=job_id)


def _store_description(ctx: AppContext, job_id: int, source: str, text: str) -> dict:
    facts = extract_facts(text)
    extractor = EXTRACTOR_VERSION
    notes: list[str] = []
    if ctx.llm_allowed():
        ctx.llm_calls_this_cycle += 1
        facts, notes = llm_extract.refine(facts, text, ctx.llm)
        if any("from LLM" in n for n in notes):
            extractor = f"{EXTRACTOR_VERSION}+llm:{ctx.llm.model_id}"
    res = add_enrichment(ctx.conn, job_id, source=source, fetch_status="ok", description=text,
                         facts=facts, extractor=extractor)
    index_job_description(ctx, job_id)
    analyze_job(ctx, job_id)
    return {"status": "ok", "enrichment_version": res.version, "notes": notes,
            "instructions_changed": res.instructions_changed}


def paste_description(ctx: AppContext, job_id: int, text: str) -> dict:
    """User-supplied description (when the listing can't be fetched)."""
    text = text.strip()
    if len(text) < 40:
        return {"status": "error", "error": "description too short"}
    return _store_description(ctx, job_id, "paste", text)


def fetch_description(ctx: AppContext, job_id: int) -> dict:
    if not ctx.settings.fetch_enabled:
        return {"status": "disabled",
                "error": "fetching is disabled (JOBRADAR_FETCH_ENABLED); paste the description"}
    job = get_job(ctx.conn, job_id)
    if not job["destination_url"]:
        return {"status": "error", "error": "no destination URL"}
    res = fetch(job["destination_url"], timeout_s=ctx.settings.fetch_timeout_s,
                max_bytes=ctx.settings.fetch_max_bytes, transport=ctx.fetch_transport)
    if res.status != "ok" or not res.text:
        # Inaccessible ≠ closed: availability stays as it was.
        add_enrichment(ctx.conn, job_id, source="fetch", fetch_status=res.status,
                       description=None)
        return {"status": res.status}
    text = html_to_text(res.text) if "<" in res.text[:2000] else res.text
    return _store_description(ctx, job_id, "fetch", text)


def enrich_pending(ctx: AppContext) -> int:
    """Fetch descriptions for qualifying jobs that have none, capped per cycle. Jobs whose
    last fetch attempt failed are not retried automatically (use Paste description)."""
    if not ctx.settings.fetch_enabled:
        return 0
    rows = ctx.conn.execute(
        "SELECT j.id FROM jobs j JOIN match_results m ON m.job_id = j.id AND "
        "m.invalidated_at IS NULL WHERE m.decision IN ('candidate', 'needs_review') AND "
        "j.availability != 'closed' AND NOT EXISTS (SELECT 1 FROM enrichments e WHERE "
        "e.job_id = j.id) ORDER BY m.rank_score DESC LIMIT ?",
        (ctx.settings.fetch_max_per_cycle,)).fetchall()
    for r in rows:
        try:
            fetch_description(ctx, r["id"])
        except Exception:
            log.exception("enrichment of job %s failed", r["id"])
    return len(rows)


# ------------------------------------------------------------------ analysis
def analyze_job(ctx: AppContext, job_id: int) -> dict:
    conn = ctx.conn
    job = get_job(conn, job_id)
    enr = latest_enrichment(conn, job_id)
    description = enr["description"] if enr else None
    ji = JobInput(
        title=job["title"], location=job["location"], work_mode=job["work_mode"],
        description=description, availability=job["availability"],
        sponsorship=enr["sponsorship"] if enr else "unknown",
        remote_scope=loads(enr["remote_scope_json"], {"status": "unknown"}) if enr
        else {"status": "unknown"},
        years_min=enr["years_min"] if enr else None,
        seniority_text=enr["seniority_text"] if enr else None,
    )
    try:
        d = decide(ji, ctx.prefs)
        explanation = None
        if d.decision in NOTIFY_DECISIONS:
            explanation = explain(conn, ctx.embedder, job, description, job_id)
        score = rank(d, explanation, job["first_published_at"]) \
            if d.decision in NOTIFY_DECISIONS else None
        conf = evidence_confidence(d, bool(description))
        decision, reasons, unknowns = d.decision, d.reasons, d.unknowns
        family, seniority, eligibility = d.role_family, d.seniority, d.eligibility
    except Exception as e:  # recorded as blocked, never fabricated
        log.exception("analysis of job %s failed", job_id)
        decision, reasons, unknowns = "blocked", [f"analysis failed: {type(e).__name__}"], []
        family = seniority = None
        eligibility, explanation, score, conf = "unknown", None, None, "low"

    with transaction(conn, immediate=True):
        conn.execute("UPDATE match_results SET invalidated_at = ?, invalidation_reason = "
                     "'superseded' WHERE job_id = ? AND invalidated_at IS NULL", (iso(), job_id))
        conn.execute(
            "INSERT INTO match_results (job_id, decision, role_family, seniority, eligibility, "
            "reasons_json, unknowns_json, explanation_json, rank_score, evidence_confidence, "
            "analyzer_version, model_version, enrichment_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, decision, family, seniority, eligibility, dumps(reasons), dumps(unknowns),
             dumps(explanation) if explanation else None, score, conf, RULES_VERSION,
             ctx.embedder.model_id if ctx.embedder else None, enr["id"] if enr else None, iso()))
        conn.execute("UPDATE jobs SET role_family = ?, seniority = ?, updated_at = ? WHERE id = ?",
                     (family, seniority, iso(), job_id))
        if decision in NOTIFY_DECISIONS:
            _sync_application(conn, job, determine_route(job["destination_url"], description))
        _sync_notification(conn, job_id, decision in NOTIFY_DECISIONS)
    return {"decision": decision, "reasons": reasons, "unknowns": unknowns, "score": score}


def _route_state(job, route: Route) -> tuple[str, str]:
    if job["availability"] == "closed":
        return "needs_review", "listing reported closed; application actions suppressed"
    if route.kind == "email":
        return "needs_review", "email route found — tap Prepare email to build a preview"
    if route.kind == "linkedin":
        return "manual_action", "open the LinkedIn listing and complete the application there"
    if route.kind == "external_form":
        return "manual_action", "complete the employer's form, then tap I applied manually"
    if route.kind == "choice":
        return "needs_review", "both email and form are offered — choose one"
    return "needs_review", "application route unknown — inspect the listing or paste instructions"


def _sync_application(conn, job, route: Route) -> None:
    app = get_application(conn, job["id"])
    state, reason = _route_state(job, route)
    now = iso()
    if app is None:
        conn.execute(
            "INSERT INTO applications (job_id, route, route_target, route_evidence, "
            "route_options_json, state, state_reason, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job["id"], route.kind, route.target, route.evidence, dumps(route.options), state,
             reason, now, now))
        return
    if app["state"] in APPLIED or app["state"] in IN_FLIGHT or app["state"] == "skipped":
        return  # history wins; availability/route changes are shown separately on the card
    user_chose = route.kind == "choice" and app["route"] in ("email", "external_form") \
        and app["route_options_json"] != "[]"
    if not user_chose:
        conn.execute(
            "UPDATE applications SET route = ?, route_target = ?, route_evidence = ?, "
            "route_options_json = ?, updated_at = ? WHERE id = ?",
            (route.kind, route.target, route.evidence, dumps(route.options), now, app["id"]))
    elif job["availability"] != "closed":
        return
    if app["state"] == "ready_for_approval" and job["availability"] != "closed":
        return  # a valid approval-ready draft stays; edits invalidate it via the store layer
    if (state, reason) != (app["state"], app["state_reason"]):
        transition(conn, app["id"], state, "system", reason)


def _sync_notification(conn, job_id: int, qualifies: bool) -> None:
    row = conn.execute("SELECT id FROM notifications WHERE job_id = ?", (job_id,)).fetchone()
    now = iso()
    if row is None:
        if qualifies:
            conn.execute(
                "INSERT INTO notifications (job_id, state, next_attempt_at, created_at, updated_at)"
                " VALUES (?, 'pending', ?, ?, ?)", (job_id, now, now, now))
        return
    mark_card_dirty(conn, job_id)


def mark_card_dirty(conn, job_id: int) -> None:
    """Ask the outbox to re-render an existing card (no-op if the job never qualified)."""
    conn.execute(
        "UPDATE notifications SET state = CASE WHEN state = 'digest' THEN 'digest' ELSE 'pending' "
        "END, next_attempt_at = ?, updated_at = ? WHERE job_id = ?", (iso(), iso(), job_id))


def reanalyze_all(ctx: AppContext) -> int:
    ids = [r[0] for r in ctx.conn.execute("SELECT id FROM jobs")]
    for job_id in ids:
        analyze_job(ctx, job_id)
    return len(ids)


def current_match(conn, job_id: int):
    return conn.execute("SELECT * FROM match_results WHERE job_id = ? AND invalidated_at IS NULL",
                        (job_id,)).fetchone()
