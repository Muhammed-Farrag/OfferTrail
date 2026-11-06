"""Structured filtering and eligibility. Order matters (see think.md §5):

1. drop announcements and clearly unrelated roles,
2. apply explicit eligibility constraints — a known mismatch is final, similarity can't rescue it,
3. classify family/seniority; unknown stays unknown (not junior; remote ≠ worldwide).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jobradar.config import Preferences
from jobradar.match.classify import (
    CORE_FAMILIES,
    classify_family,
    classify_seniority,
    description_establishes_ai_work,
)
from jobradar.match.geo import region_contains, resolve_country

RULES_VERSION = "rules@v1"


@dataclass
class JobInput:
    title: str | None
    location: str | None
    work_mode: str = "unknown"
    description: str | None = None
    availability: str = "unknown"
    sponsorship: str = "unknown"
    remote_scope: dict = field(default_factory=lambda: {"status": "unknown"})
    years_min: int | None = None
    seniority_text: str | None = None


@dataclass
class Decision:
    decision: str  # candidate | needs_review | uncertain | excluded | ignored
    role_family: str
    seniority: str
    eligibility: str  # eligible | excluded | unknown
    reasons: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    sponsorship_confirmed: bool = False


def _eligibility(job: JobInput, prefs: Preferences) -> tuple[str, list[str], list[str], bool]:
    """Returns (status, reasons, unknowns, sponsorship_confirmed)."""
    reasons: list[str] = []
    unknowns: list[str] = []
    home = prefs.residence_country
    country = resolve_country(job.location)
    mode = job.work_mode

    if prefs.work_modes is not None and mode != "unknown" and mode not in prefs.work_modes:
        return "excluded", [f"work mode {mode} not in your preferences"], [], False
    if home is None:
        return "unknown", [], ["your residence country is not set (/settings)"], False

    if mode == "remote":
        if country == home:
            return "eligible", [f"remote role based in {home}"], [], False
        scope = job.remote_scope or {"status": "unknown"}
        allowed = scope.get("allowed", [])
        accepts_home = any(a == home or region_contains(a, home) for a in allowed)
        if scope.get("status") in ("open", "restricted") and accepts_home:
            return "eligible", [f"remote scope includes {home} ({', '.join(allowed)})"], [], False
        if scope.get("status") == "restricted":
            return "excluded", [f"remote restricted to {', '.join(allowed)}"], [], False
        where = f" ({job.location})" if job.location else ""
        unknowns.append(f"cross-border remote eligibility unknown{where}")
        return "unknown", reasons, unknowns, False

    # on-site, hybrid or unknown mode: depends on where the job is.
    if country is None:
        unknowns.append("job country unknown")
        if mode == "unknown":
            unknowns.append("work mode unknown")
        return "unknown", reasons, unknowns, False
    if country == home:
        return "eligible", [f"located in {home}"], [], False
    if mode == "unknown":
        unknowns.append("work mode unknown (could be remote)")
    if not prefs.require_sponsorship_abroad:
        return "eligible", [f"located in {country}; sponsorship not required by your settings"], \
            unknowns, False
    if job.sponsorship == "yes":
        return "eligible", [f"located in {country}; visa sponsorship stated"], unknowns, True
    if job.sponsorship == "no":
        return "excluded", [f"located in {country}; no visa sponsorship"], [], False
    unknowns.append("⚠️ visa sponsorship unknown")
    return "unknown", reasons, unknowns, False


def decide(job: JobInput, prefs: Preferences, kind: str = "job") -> Decision:
    if kind != "job":
        return Decision("ignored", "unrelated", "unknown", "unknown", [f"non-job message ({kind})"])

    family = classify_family(job.title, job.description)
    seniority = classify_seniority(job.title, job.description)
    if seniority == "unknown" and job.seniority_text:
        seniority = {"entry-level": "junior", "entry level": "junior", "graduate": "junior",
                     "mid-level": "mid", "mid level": "mid",
                     "experienced": "mid"}.get(job.seniority_text, job.seniority_text)

    if family == "unrelated":
        return Decision("ignored", family, seniority, "unknown", ["unrelated role"])

    reasons: list[str] = []
    unknowns: list[str] = []

    # Category gates
    if family == "evaluation" and not prefs.include_evaluation_gigs:
        return Decision("excluded", family, seniority, "unknown",
                        ["AI evaluation/annotation gig (opt-in category, disabled)"])
    if family == "data_science" and not prefs.include_data_science:
        return Decision("excluded", family, seniority, "unknown",
                        ["data science role (optional family, disabled)"])

    # Experience gates (only on explicit evidence)
    ceiling = prefs.max_years_experience
    if seniority in ("senior", "mid") and ceiling is not None and ceiling <= 2:
        return Decision("excluded", family, seniority, "unknown",
                        [f"{seniority} role; your experience ceiling is {ceiling} years"])
    if job.years_min is not None and ceiling is not None and job.years_min > ceiling:
        return Decision("excluded", family, seniority, "unknown",
                        [f"requires {job.years_min}+ years; your ceiling is {ceiling}"])
    if seniority == "intern" and prefs.include_internships is False:
        return Decision("excluded", family, seniority, "unknown", ["internship; disabled"])

    if job.availability == "closed":
        reasons.append("listing reported closed")

    elig, e_reasons, e_unknowns, sponsored = _eligibility(job, prefs)
    reasons += e_reasons
    unknowns += e_unknowns
    if elig == "excluded":
        return Decision("excluded", family, seniority, elig, reasons, unknowns)

    if seniority == "unknown":
        unknowns.append("seniority not stated")
    if ceiling is None:
        unknowns.append("your experience ceiling is not set (/settings)")
    if seniority == "intern" and prefs.include_internships is None:
        unknowns.append("internship preference not set")

    if family == "software_generic":
        if description_establishes_ai_work(job.description):
            reasons.append("generic title, but the description describes AI/ML work")
            family_ok = True
        else:
            return Decision("uncertain", family, seniority, elig,
                            reasons + ["generic software role; no AI work established"], unknowns)
    else:
        family_ok = family in prefs.target_families or family in ("data_science", "evaluation")
        if not family_ok and family in CORE_FAMILIES:
            return Decision("excluded", family, seniority, elig,
                            [f"{family} not in your target families"])

    ready = elig == "eligible" and not unknowns and family_ok
    decision = "candidate" if ready else "needs_review"
    return Decision(decision, family, seniority, elig, reasons, unknowns, sponsored)
