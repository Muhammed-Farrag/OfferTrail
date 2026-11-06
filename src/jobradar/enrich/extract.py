"""Rule-based extraction of decision-relevant facts from a job description.

Every extracted value carries the excerpt it came from. Anything not found stays unknown.
Description text is untrusted data: it is only pattern-matched here, never executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from jobradar.match.classify import years_required
from jobradar.match.geo import REGIONS, countries_in

EXTRACTOR_VERSION = "rules@v1"


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template", "svg"}
    _BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "tr", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


_CLOSED = re.compile(
    r"no longer accepting applications|this job is no longer available|"
    r"(?:position|role|vacancy) (?:has been|is) (?:filled|closed)|applications? (?:are|is) (?:now )?closed|"
    r"job (?:posting )?has expired|this (?:job|position) has been closed",
    re.I,
)
_SPONSOR_NO = re.compile(
    r"(?:no|not|unable to|cannot|can't|do not|don't|does not|will not|won't)\s+(?:\w+\s+){0,3}?"
    r"(?:offer\s+|provide\s+)?(?:visa\s+)?sponsor(?:ship)?|without (?:visa )?sponsorship|"
    r"must (?:already )?(?:have|hold) (?:the |a )?(?:valid )?(?:right|authori[sz]ation|permit) to work|"
    r"sponsorship (?:is )?not (?:available|provided|offered)",
    re.I,
)
_SPONSOR_YES = re.compile(
    r"visa sponsorship (?:is )?(?:available|provided|offered|included)|"
    r"(?:we|will|we will|company will) (?:offer|provide|support) (?:full )?(?:visa|work permit) sponsorship|"
    r"(?:we|will) sponsor (?:your |the |a )?(?:work )?visa|"
    r"relocation (?:and|&) visa (?:support|sponsorship)|visa (?:support|sponsorship) (?:and|&) relocation|"
    r"sponsorship (?:is )?available",
    re.I,
)
_WORLDWIDE = re.compile(
    r"work from anywhere|anywhere in the world|worldwide|globally remote|remote[- ]first,? global|"
    r"open to (?:candidates|applicants) (?:from|in) (?:any|all) (?:country|countries|locations?)",
    re.I,
)
_REGION_NAMES = "|".join(sorted((re.escape(r) for r in REGIONS if r != "worldwide"), key=len,
                                reverse=True))
_REMOTE_REGION = re.compile(
    r"remote\b[^.\n]{0,30}?\b(" + _REGION_NAMES + r")\b",
    re.I,
)
_RESTRICTED = re.compile(
    r"(?:must|should|need to) (?:be )?(?:currently )?(?:located|based|residing|reside|live) in\s+"
    r"([^.\n;]{2,80})|"
    r"only (?:candidates|applicants) (?:who are )?(?:located|based|residing) in\s+([^.\n;]{2,80})|"
    r"remote (?:within|in) (?:the )?([A-Z][^.\n;,()]{1,40}) only",
    re.I,
)
_OPEN_TO = re.compile(
    r"open to (?:candidates|applicants|talent) (?:located |based )?(?:in|from)\s+([^.\n;]{2,80})|"
    r"(?:candidates|applicants) (?:from|in)\s+([^.\n;]{2,80}?) (?:are )?(?:welcome|encouraged|eligible)",
    re.I,
)
_SENIORITY_STATED = re.compile(
    r"(?:this is an?|seeking an?|looking for an?|hiring an?)\s+"
    r"(junior|entry[- ]level|graduate|senior|mid[- ]level|experienced)\b",
    re.I,
)
_APPLY_SENTENCE = re.compile(
    r"[^.\n]*\b(?:apply|application|applications|cv|resume|résumé)\b[^.\n]*"
    r"\b(?:email|e-mail|send|submit|form|link|portal|website|via|through|at)\b[^.\n]*[.\n]?",
    re.I,
)
_EMPLOYER = re.compile(
    r"(?:on behalf of|hiring for|our client|recruiting for|in partnership with|powered by)\s+"
    r"([A-Za-z0-9][\w.&-]{1,40})|"
    r"\b([A-Za-z0-9][\w.&-]{1,40}) is (?:hiring|looking for|seeking)",
    re.I,
)
_GENERIC_EMPLOYER_WORDS = {"we", "who", "company", "team", "client", "the", "our", "this", "it"}


@dataclass
class JobFacts:
    availability: str = "unknown"
    sponsorship: str = "unknown"
    remote_scope: dict = field(default_factory=lambda: {"status": "unknown"})
    years_min: int | None = None
    seniority_text: str | None = None
    apply_instructions: str | None = None
    stated_employer: str | None = None
    excerpts: list[dict] = field(default_factory=list)

    def add(self, field_name: str, excerpt: str) -> None:
        self.excerpts.append({"field": field_name, "excerpt": excerpt.strip()[:300]})


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    right_candidates = [i for i in (text.find(".", end), text.find("\n", end)) if i != -1]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    return text[left:right].strip()


def extract_facts(description: str) -> JobFacts:
    text = description
    f = JobFacts()

    if m := _CLOSED.search(text):
        f.availability = "closed"
        f.add("availability", _sentence_around(text, m.start(), m.end()))

    if m := _SPONSOR_NO.search(text):
        f.sponsorship = "no"
        f.add("sponsorship", _sentence_around(text, m.start(), m.end()))
    elif m := _SPONSOR_YES.search(text):
        f.sponsorship = "yes"
        f.add("sponsorship", _sentence_around(text, m.start(), m.end()))

    scope: dict = {"status": "unknown"}
    if m := _RESTRICTED.search(text):
        span = next(g for g in m.groups() if g)
        codes = countries_in(span)
        regions = [r for r in REGIONS if re.search(rf"\b{re.escape(r)}\b", span, re.I)]
        if codes or regions:
            scope = {"status": "restricted", "allowed": codes + regions}
            f.add("remote_scope", _sentence_around(text, m.start(), m.end()))
    if scope["status"] == "unknown":
        if m := _WORLDWIDE.search(text):
            scope = {"status": "open", "allowed": ["worldwide"]}
            f.add("remote_scope", _sentence_around(text, m.start(), m.end()))
        elif m := _REMOTE_REGION.search(text):
            scope = {"status": "open", "allowed": [m.group(1).lower()]}
            f.add("remote_scope", _sentence_around(text, m.start(), m.end()))
        elif m := _OPEN_TO.search(text):
            span = next(g for g in m.groups() if g)
            codes = countries_in(span)
            regions = [r for r in REGIONS if re.search(rf"\b{re.escape(r)}\b", span, re.I)]
            if codes or regions:
                scope = {"status": "open", "allowed": codes + regions}
                f.add("remote_scope", _sentence_around(text, m.start(), m.end()))
    f.remote_scope = scope

    years, excerpt = years_required(text)
    if years is not None:
        f.years_min = years
        f.add("years", excerpt or "")

    if m := _SENIORITY_STATED.search(text):
        f.seniority_text = m.group(1).lower()
        f.add("seniority", _sentence_around(text, m.start(), m.end()))

    apply_sentences = [s.group(0).strip() for s in _APPLY_SENTENCE.finditer(text)][:4]
    if apply_sentences:
        f.apply_instructions = "\n".join(apply_sentences)
        for s in apply_sentences:
            f.add("apply_instructions", s)

    for m in _EMPLOYER.finditer(text):
        name = (m.group(1) or m.group(2) or "").strip(".")
        if name and name.lower() not in _GENERIC_EMPLOYER_WORDS:
            f.stated_employer = name
            f.add("stated_employer", _sentence_around(text, m.start(), m.end()))
            break

    return f
