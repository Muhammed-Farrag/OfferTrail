"""Grounded chat answers.

* Dates, statuses and counts → SQL (never the model).
* "Why does this match me?" / "What is missing?" → the stored match explanation, whose every
  strength cites an approved-fact chunk ID.
* Anything else → retrieval over both collections; the model may summarise only the retrieved
  chunks and must cite their IDs. Citations to unknown IDs are dropped; no evidence → unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from jobradar.apply.state import DISPLAY
from jobradar.context import AppContext
from jobradar.llm.base import LLMUnavailable, strict_object
from jobradar.rag.store import search
from jobradar.util import iso, loads, utcnow

ANSWER_SYSTEM = (
    "Answer the user's question about their job search using ONLY the evidence chunks given. "
    "Cite the chunk IDs you relied on. If the evidence does not answer the question, say so and "
    "list what is unknown. Never invent qualifications, requirements or facts."
)
ANSWER_SCHEMA = strict_object({
    "answer": {"type": "string"},
    "citations": {"type": "array", "items": {"type": "string"}},
    "unknowns": {"type": "array", "items": {"type": "string"}},
})


@dataclass
class Answer:
    text: str
    kind: str  # sql | explanation | rag | unknown | blocked
    citations: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)


_JOB_REF = re.compile(r"(?:#|\bjob\s*)(\d+)\b", re.I)


def _job_ref(question: str, focus_job: int | None) -> int | None:
    m = _JOB_REF.search(question)
    return int(m.group(1)) if m else focus_job


def list_jobs(ctx: AppContext, *, days: int | None = None, junior_only: bool = False,
              decisions: tuple[str, ...] = ("candidate", "needs_review"), limit: int = 20
              ) -> list[dict]:
    placeholders = ",".join("?" * len(decisions))
    sql = ("SELECT j.id, j.title, j.publisher, j.location, j.availability, j.first_published_at,"
           " m.decision, m.seniority, m.role_family, m.rank_score, a.state FROM jobs j "
           "JOIN match_results m ON m.job_id = j.id AND m.invalidated_at IS NULL "
           f"LEFT JOIN applications a ON a.job_id = j.id WHERE m.decision IN ({placeholders})")
    args: list = list(decisions)
    if days is not None:
        sql += " AND j.first_published_at >= ?"
        args.append(iso(utcnow() - timedelta(days=days)))
    if junior_only:
        sql += " AND m.seniority IN ('junior', 'intern')"
    sql += " ORDER BY (j.availability = 'closed'), m.rank_score DESC, j.id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in ctx.conn.execute(sql, args)]


def applications_in(ctx: AppContext, states: tuple[str, ...]) -> list[dict]:
    return [dict(r) for r in ctx.conn.execute(
        "SELECT a.job_id, a.state, a.state_reason, a.route, a.route_target, a.updated_at, "
        "j.title, j.publisher FROM applications a JOIN jobs j ON j.id = a.job_id "
        "WHERE a.state IN ({}) ORDER BY a.updated_at DESC".format(",".join("?" * len(states))),
        states)]


def _fmt_jobs(rows: list[dict]) -> str:
    if not rows:
        return "No matching jobs in the database."
    out = []
    for r in rows:
        closed = " ⛔ closed" if r["availability"] == "closed" else ""
        out.append(f"#{r['id']} {r['title']} — {r['publisher'] or 'unknown company'} "
                   f"({r['seniority']}, {r['decision']}, score {r['rank_score']}){closed}")
    return "\n".join(out)


def answer(ctx: AppContext, question: str, focus_job: int | None = None) -> Answer:
    q = question.strip()
    low = q.lower()

    # ---- SQL intents -------------------------------------------------------
    if re.search(r"manual (work|action|step)|need(s)? manual", low):
        rows = applications_in(ctx, ("manual_action",))
        text = "\n".join(f"#{r['job_id']} {r['title']} — {r['state_reason']}" for r in rows) \
            or "No applications are waiting for manual work."
        return Answer(text, "sql", rows=rows)
    if re.search(r"\bhow many\b", low):
        counts = {r["state"]: r["n"] for r in ctx.conn.execute(
            "SELECT state, COUNT(*) AS n FROM applications GROUP BY state")}
        jobs = ctx.conn.execute(
            "SELECT COUNT(*) FROM match_results WHERE invalidated_at IS NULL AND decision IN "
            "('candidate','needs_review')").fetchone()[0]
        lines = [f"Qualifying jobs: {jobs}"] + [f"{DISPLAY[s]}: {n}" for s, n in sorted(
            counts.items())]
        return Answer("\n".join(lines), "sql", rows=[{"jobs": jobs, **counts}])
    if re.search(r"\b(show|list|find|any)\b.*\bjobs?\b|\bjobs?\b.*\b(this week|today)\b", low):
        days = 1 if "today" in low else 7 if re.search(r"this week|last 7|past week", low) \
            else None
        rows = list_jobs(ctx, days=days, junior_only=bool(re.search(r"junior|entry|intern", low)))
        return Answer(_fmt_jobs(rows), "sql", rows=rows)
    if re.search(r"\b(applied|pending)\b.*\b(list|show|which)\b|\bwhich\b.*\bapplied\b", low):
        rows = applications_in(ctx, ("applied_gmail", "applied_manual"))
        text = "\n".join(f"#{r['job_id']} {r['title']} — {DISPLAY[r['state']]}" for r in rows) \
            or "No applications recorded as applied."
        return Answer(text, "sql", rows=rows)

    # ---- explanation intents ----------------------------------------------
    if re.search(r"why .*match|why .*(recommend|suggest)|missing|lack|gap|what do i need", low):
        job_id = _job_ref(q, focus_job)
        if job_id is None:
            return Answer("Which job? Mention it as #<id> or open its card first.", "unknown",
                          unknowns=["job not specified"])
        return explain_job(ctx, job_id, missing_only=bool(re.search(r"missing|lack|gap|need",
                                                                    low)))

    # ---- general retrieval --------------------------------------------------
    return retrieval_answer(ctx, q, _job_ref(q, focus_job))


def explain_job(ctx: AppContext, job_id: int, missing_only: bool = False) -> Answer:
    m = ctx.conn.execute("SELECT * FROM match_results WHERE job_id = ? AND invalidated_at IS NULL",
                         (job_id,)).fetchone()
    if m is None:
        return Answer(f"Job #{job_id} has no current analysis.", "unknown",
                      unknowns=["no analysis"])
    exp = loads(m["explanation_json"], None)
    reasons, unknowns = loads(m["reasons_json"], []), loads(m["unknowns_json"], [])
    if exp is None:
        return Answer(f"Job #{job_id} was not explained (decision: {m['decision']}; "
                      f"{'; '.join(reasons)}).", "explanation", unknowns=unknowns)
    lines, cites = [], []
    if not missing_only:
        lines.append(f"Decision: {m['decision']} · family {m['role_family']} · seniority "
                     f"{m['seniority']} · eligibility {m['eligibility']}")
        if reasons:
            lines.append("Reasons: " + "; ".join(reasons))
        if exp["strengths"]:
            lines.append("Strengths (from your approved facts):")
            for s in exp["strengths"]:
                lines.append(f"• {s['requirement']}: {s['text']} [{s['chunk_id']}]")
                cites.append(s["chunk_id"])
    if exp["missing"]:
        lines.append("Not found in your approved facts: " + ", ".join(exp["missing"]))
    elif exp["requirements_source"] == "description":
        lines.append("No listed requirement is missing from your approved facts.")
    all_unknowns = unknowns + exp["unknowns"]
    if all_unknowns:
        lines.append("Unknown: " + "; ".join(all_unknowns))
    return Answer("\n".join(lines), "explanation", cites, all_unknowns)


def retrieval_answer(ctx: AppContext, question: str, job_id: int | None) -> Answer:
    if ctx.embedder is None:
        return Answer("Retrieval is unavailable (no embedding backend).", "blocked")
    floor = ctx.embedder.min_relevance
    hits = search(ctx.conn, ctx.embedder, "candidate", question, k=4, min_score=floor)
    if job_id is not None:
        hits += search(ctx.conn, ctx.embedder, "job", question, k=3, job_id=job_id,
                       min_score=floor)
    if not hits:
        return Answer("Unknown — no stored evidence covers this question.", "unknown",
                      unknowns=[question])
    evidence = [{"id": h.chunk_id, "text": h.text} for h in hits]
    ids = {e["id"] for e in evidence}
    if not ctx.llm_allowed():
        text = "LLM unavailable — showing the retrieved evidence only:\n" + "\n".join(
            f"• {e['text']} [{e['id']}]" for e in evidence)
        return Answer(text, "rag", sorted(ids))
    ctx.llm_calls_this_cycle += 1
    try:
        out = ctx.llm.complete_json(task="grounded_answer", system=ANSWER_SYSTEM,
                                    schema=ANSWER_SCHEMA,
                                    data={"question": question, "evidence": evidence})
    except LLMUnavailable as e:
        text = f"LLM unavailable ({e}) — retrieved evidence:\n" + "\n".join(
            f"• {e2['text']} [{e2['id']}]" for e2 in evidence)
        return Answer(text, "rag", sorted(ids))
    cites = [c for c in out.get("citations", []) if c in ids]
    if not cites:
        return Answer("Unknown — the model could not ground an answer in stored evidence.",
                      "unknown", unknowns=out.get("unknowns", []) or [question])
    return Answer(out["answer"], "rag", cites, out.get("unknowns", []))
