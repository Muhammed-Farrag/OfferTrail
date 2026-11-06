"""Application route detection, independent of match quality.

An email address becomes an application recipient only when the sentence containing it
explicitly tells candidates to apply / send their CV there. Addresses in support,
accommodation, privacy or otherwise non-application context are rejected, as are sentences
that look like injected instructions. Opening a link is never evidence of applying.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from jobradar.parse.linkedin import is_linkedin_host

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL = re.compile(r"https?://[^\s<>\"')\]]+")

APPLY_BY_EMAIL = re.compile(
    r"(?:to apply|apply|applications?|interested candidates?|interested applicants?|candidates)"
    r"[^.\n]{0,60}?(?:send|e-?mail|submit|forward|share)[^.\n]{0,40}?"
    r"(?:cv|resume|résumé|application|portfolio)?[^.\n]{0,20}?(?:to|at|via)\s*$"
    r"|(?:send|e-?mail|submit|forward|share)\s+(?:us\s+)?(?:your|an?)\s+(?:updated\s+)?"
    r"(?:cv|resume|résumé|application)[^.\n]{0,40}?(?:to|at)\s*$"
    r"|apply (?:by|via) (?:sending an? )?e-?mail(?:ing)?(?: (?:us|your cv|your resume))?"
    r"(?: to| at)?\s*:?\s*$",
    re.I,
)
NON_APPLICATION_CONTEXT = re.compile(
    r"accommodat|accessib|disabilit|assistance|support|help\s*desk|privacy|gdpr|data protection|"
    r"unsubscribe|report|abuse|fraud|scam|questions?|inquir|enquir|press|media|billing|invoice|"
    r"complain|feedback|legal",
    re.I,
)
INJECTION_MARKERS = re.compile(
    r"ignore (?:all |any |the )?(?:previous|prior|above)|system prompt|you are (?:now )?an? |"
    r"assistant[,:]|api[_ ]?keys?|tokens?|passwords?|credentials?|secrets?|session|bot token|"
    r"disregard|override|jailbreak",
    re.I,
)
FORM_CONTEXT = re.compile(
    r"apply (?:here|now|online|at|via|through|using)|application form|apply on our (?:website|site|"
    r"careers page|portal)|fill (?:out|in) (?:the|our|this) form|submit (?:your )?application (?:at|"
    r"via|through)",
    re.I,
)
SUBJECT = re.compile(
    r"(?:subject(?: line)?|with the subject|title (?:your|the) e-?mail)\s*[:\-]?\s*[\"“'‘]([^\"”'’\n]{2,120})[\"”'’]"
    r"|(?:quote|include|mention) (?:the )?(?:job )?(?:reference|ref\.?|code|id)\s*[:#]?\s*([A-Z0-9][\w/-]{1,40})",
    re.I,
)


@dataclass
class EmailCandidate:
    address: str
    sentence: str
    accepted: bool
    reason: str


@dataclass
class Route:
    kind: str  # email | linkedin | external_form | unknown | choice
    target: str | None = None
    evidence: str = ""
    required_subject: str | None = None
    options: list[dict] = field(default_factory=list)
    rejected_emails: list[EmailCandidate] = field(default_factory=list)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def classify_emails(text: str) -> list[EmailCandidate]:
    out = []
    for sentence in _sentences(text):
        for m in EMAIL.finditer(sentence):
            addr = m.group(0).rstrip(".")
            before = sentence[:m.start()]
            if INJECTION_MARKERS.search(sentence):
                out.append(EmailCandidate(addr, sentence, False, "suspicious instruction text"))
            elif NON_APPLICATION_CONTEXT.search(sentence):
                out.append(EmailCandidate(addr, sentence, False,
                                          "support/accommodation/privacy context"))
            elif APPLY_BY_EMAIL.search(before.rstrip(" :")):
                out.append(EmailCandidate(addr, sentence, True, "explicit apply-by-email"))
            else:
                out.append(EmailCandidate(addr, sentence, False,
                                          "no explicit instruction to apply by email"))
    return out


def _forms(text: str) -> list[tuple[str, str]]:
    found = []
    for sentence in _sentences(text):
        if not FORM_CONTEXT.search(sentence) or INJECTION_MARKERS.search(sentence):
            continue
        for u in URL.findall(sentence):
            u = u.rstrip(".,;")
            host = urlsplit(u).hostname or ""
            if not is_linkedin_host(host):
                found.append((u, sentence))
    return found


def required_subject(text: str | None) -> str | None:
    if not text:
        return None
    m = SUBJECT.search(text)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


def determine_route(destination_url: str | None, description: str | None) -> Route:
    emails = classify_emails(description) if description else []
    accepted = [e for e in emails if e.accepted]
    rejected = [e for e in emails if not e.accepted]
    forms = _forms(description) if description else []
    subject = required_subject(description)

    if accepted and forms:
        e, (form_url, form_sentence) = accepted[0], forms[0]
        return Route(
            "choice",
            evidence="Both an application email and a form are given; choose one.",
            required_subject=subject,
            options=[{"kind": "email", "target": e.address, "evidence": e.sentence},
                     {"kind": "external_form", "target": form_url, "evidence": form_sentence}],
            rejected_emails=rejected)
    if accepted:
        e = accepted[0]
        return Route("email", e.address, e.sentence, subject, rejected_emails=rejected)
    if forms:
        url, sentence = forms[0]
        return Route("external_form", url, sentence, rejected_emails=rejected)
    if destination_url and is_linkedin_host(urlsplit(destination_url).hostname or ""):
        return Route("linkedin", destination_url,
                     "Alert links to a LinkedIn listing; complete the application there. "
                     "Easy Apply availability is not known.", rejected_emails=rejected)
    if destination_url:
        return Route("unknown", destination_url,
                     "Destination is not a known application form; inspect it or paste the "
                     "application instructions.", rejected_emails=rejected)
    return Route("unknown", None, "No application instructions found.",
                 rejected_emails=rejected)
