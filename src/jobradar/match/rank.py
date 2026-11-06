"""Ranking heuristic and evidence confidence — two separate numbers, neither a hiring
probability.

rank_score (0–100) = family fit (0–35) + seniority fit (0–25) + eligibility (0–25)
                     + skill overlap (0–10) + freshness (0–5)

evidence_confidence reflects how much of the decision rests on stated evidence:
high = description stored and seniority + eligibility both known; low = alert text only.
"""

from __future__ import annotations

from datetime import timedelta

from jobradar.match.rules import Decision
from jobradar.util import parse_iso, utcnow

_FAMILY = {"genai": 35, "ai_ml": 35, "research_eng": 28, "data_science": 20, "evaluation": 12,
           "software_generic": 10}
_SENIORITY = {"junior": 25, "intern": 22, "unknown": 10, "mid": 0, "senior": 0}
_ELIG = {"eligible": 25, "unknown": 8, "excluded": 0}


def rank(decision: Decision, explanation: dict | None, published_at: str | None) -> float:
    score = _FAMILY.get(decision.role_family, 0) + _SENIORITY.get(decision.seniority, 0)
    score += _ELIG.get(decision.eligibility, 0)
    if decision.sponsorship_confirmed:
        score += 3
    if explanation:
        total = len(explanation["strengths"]) + len(explanation["missing"])
        if total:
            score += 10 * len(explanation["strengths"]) / total
    if published_at:
        age = utcnow() - parse_iso(published_at)
        if age <= timedelta(days=2):
            score += 5
        elif age <= timedelta(days=7):
            score += 2
    return round(min(score, 100.0), 1)


def evidence_confidence(decision: Decision, has_description: bool) -> str:
    known = sum([decision.seniority != "unknown", decision.eligibility != "unknown",
                 has_description])
    return {3: "high", 2: "medium"}.get(known, "low")
