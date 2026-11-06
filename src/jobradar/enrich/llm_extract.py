"""Optional LLM refinement of job facts the rules left unknown.

The model must quote the sentence each value comes from; a value is accepted only if that
quote really occurs in the stored description. Unverified values are discarded, so the model
can fill gaps but cannot invent requirements.
"""

from __future__ import annotations

import re

from jobradar.enrich.extract import JobFacts
from jobradar.llm.base import LLMProvider, LLMUnavailable, strict_object
from jobradar.match.geo import REGIONS

SYSTEM = (
    "You extract hiring facts from a job description. Return only what the text states. "
    "For every value, copy the exact supporting sentence into the matching *_quote field. "
    "Use 'unknown', [] or null when the text does not state it."
)

SCHEMA = strict_object({
    "sponsorship": {"type": "string", "enum": ["yes", "no", "unknown"]},
    "sponsorship_quote": {"type": "string"},
    "remote_scope": {"type": "array", "items": {"type": "string"}},
    "remote_scope_quote": {"type": "string"},
    "years_min": {"type": ["integer", "null"]},
    "years_quote": {"type": "string"},
})


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _verified(quote: str, description: str) -> bool:
    q = _norm(quote)
    return len(q) >= 12 and q in _norm(description)


def refine(facts: JobFacts, description: str, llm: LLMProvider) -> tuple[JobFacts, list[str]]:
    """Returns (facts, notes). Notes describe accepted/rejected model output."""
    needs = (facts.sponsorship == "unknown" or facts.remote_scope.get("status") == "unknown"
             or facts.years_min is None)
    if not needs:
        return facts, []
    notes: list[str] = []
    try:
        out = llm.complete_json(task="extract_job_facts", system=SYSTEM,
                                data={"description": description[:12000]}, schema=SCHEMA)
    except LLMUnavailable as e:
        return facts, [f"LLM extraction unavailable: {e}"]

    if facts.sponsorship == "unknown" and out.get("sponsorship") in ("yes", "no"):
        if _verified(out.get("sponsorship_quote", ""), description):
            facts.sponsorship = out["sponsorship"]
            facts.add("sponsorship", out["sponsorship_quote"])
            notes.append("sponsorship from LLM (quote verified)")
        else:
            notes.append("rejected LLM sponsorship value: quote not found in description")

    scope = [s.lower() for s in out.get("remote_scope") or []
             if isinstance(s, str) and (s.lower() in REGIONS or re.fullmatch(r"[A-Z]{2}", s))]
    if facts.remote_scope.get("status") == "unknown" and scope:
        if _verified(out.get("remote_scope_quote", ""), description):
            facts.remote_scope = {"status": "open", "allowed": [
                s.upper() if len(s) == 2 else s for s in scope]}
            facts.add("remote_scope", out["remote_scope_quote"])
            notes.append("remote scope from LLM (quote verified)")
        else:
            notes.append("rejected LLM remote scope: quote not found in description")

    years = out.get("years_min")
    if facts.years_min is None and isinstance(years, int) and 0 <= years <= 30:
        if _verified(out.get("years_quote", ""), description) and str(years) in out["years_quote"]:
            facts.years_min = years
            facts.add("years", out["years_quote"])
            notes.append("years from LLM (quote verified)")
        else:
            notes.append("rejected LLM years value: quote not verified")
    return facts, notes
