"""Role family and seniority classification from the title (and description when present).

Rules are deliberately conservative:
* "AI" in a title is not enough — the role noun must be a technical one
  ("Road Test Drivers — AI GPS Mapping" is a driver job).
* A company name containing "AI" is never considered.
* No seniority word means `unknown`, not junior.
"""

from __future__ import annotations

import re

FAMILIES = (
    "ai_ml", "genai", "research_eng", "data_science", "evaluation",
    "software_generic", "unrelated",
)
CORE_FAMILIES = ("ai_ml", "genai", "research_eng")

_W = r"(?<![a-z0-9])"  # left word boundary that also works next to punctuation
_E = r"(?![a-z0-9])"


def _rx(words: str) -> re.Pattern[str]:
    return re.compile(_W + r"(?:" + words + r")" + _E, re.I)


TECH_ROLE = _rx(
    r"engineers?|developers?|dev|scientists?|researchers?|architects?|programmers?|"
    r"interns?|internship|trainee|specialists?|analysts?|swe|mle|consultant"
)
NON_TECH_ROLE = _rx(
    r"drivers?|waiter|waitress|waiter/ess|waiters?|cashiers?|chefs?|cooks?|baristas?|nurses?|"
    r"sales\s+(?:representative|associate|executive)|receptionists?|cleaners?|"
    r"delivery|warehouse|security\s+guard|accountants?|teachers?|tutors?|recruiters?|"
    r"marketing\s+(?:specialist|manager|executive)|customer\s+service"
)
GENAI = _rx(
    r"llm|llms|genai|gen\s?ai|generative\s+ai|large\s+language\s+models?|prompt\s+engineer(?:ing)?|"
    r"rag|nlp\s+engineer|conversational\s+ai|ai\s+agents?|agentic"
)
AI_ML = _rx(
    r"ai|a\.i\.|ml|machine\s+learning|deep\s+learning|computer\s+vision|nlp|"
    r"mlops|ml\s?ops|artificial\s+intelligence|neural|data\s+science|cv\s+engineer"
)
RESEARCH = _rx(r"research\s+(?:engineer|scientist)|researcher|research")
DATA_SCIENCE = _rx(r"data\s+scientists?|data\s+science")
EVALUATION = _rx(
    r"evaluators?|annotators?|annotation|labell?ers?|labell?ing|raters?|ai\s+trainers?|"
    r"ai\s+tutors?|data\s+(?:annotation|labell?ing)|response\s+(?:rater|evaluator)"
)
SOFTWARE = _rx(
    r"software|backend|back-end|frontend|front-end|full\s?stack|full-stack|python|java|"
    r"golang|\.net|react|node(?:\.?js)?|devops|cloud|data\s+engineer|mobile|ios|android|qa|"
    r"sre|platform|web"
)

SENIOR = _rx(r"senior|sr\.?|lead|principal|staff|head|director|manager|chief|vp|expert")
MID = _rx(r"mid(?:-level)?|intermediate|ii|iii|iv")
JUNIOR = _rx(r"junior|jr\.?|entry(?:[-\s]level)?|graduate|grad|fresh(?:er)?|new\s+grad|associate")
INTERN = _rx(r"interns?|internship|trainee|apprentice(?:ship)?|co-?op")

_YEARS = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b[^.\n]{0,40}?"
    r"(?:experience|exp\b)",
    re.I,
)


def classify_family(title: str | None, description: str | None = None) -> str:
    t = title or ""
    if not t.strip():
        return "unrelated"
    if NON_TECH_ROLE.search(t) and not TECH_ROLE.search(t):
        return "unrelated"
    if EVALUATION.search(t):
        return "evaluation"
    if not TECH_ROLE.search(t):
        # e.g. "AI GPS Mapping" with no technical role noun: not an engineering role.
        return "unrelated"
    if DATA_SCIENCE.search(t):
        return "data_science"
    if RESEARCH.search(t) and AI_ML.search(t) or RESEARCH.search(t) and GENAI.search(t):
        return "research_eng"
    if GENAI.search(t):
        return "genai"
    if AI_ML.search(t):
        return "ai_ml"
    if SOFTWARE.search(t) or TECH_ROLE.search(t):
        return "software_generic"
    return "unrelated"


def description_establishes_ai_work(description: str | None) -> bool:
    """A generic software role counts as AI work only if the description says so repeatedly."""
    if not description:
        return False
    hits = len(GENAI.findall(description)) + len(AI_ML.findall(description))
    return hits >= 3


def classify_seniority(title: str | None, description: str | None = None) -> str:
    t = title or ""
    if INTERN.search(t):
        return "intern"
    if SENIOR.search(t):
        return "senior"
    if JUNIOR.search(t):
        return "junior"
    if MID.search(t):
        return "mid"
    return "unknown"


def years_required(description: str | None) -> tuple[int | None, str | None]:
    """Minimum years of experience stated in a description, with the matching excerpt."""
    if not description:
        return None, None
    m = _YEARS.search(description)
    if not m:
        return None, None
    return int(m.group(1)), m.group(0)
