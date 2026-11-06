"""Deterministic parser for channel job alerts.

Observed format (Nov 2026 sample), one field per line with emoji labels:

    AI Engineer (Remote)
    🏢 Joblet-AI
    📍 United Arab Emirates
    🕒 4 minutes ago
    📌 via LinkedIn

    ➡️ Apply Here            <- text-URL entity pointing at the LinkedIn listing

Fields that cannot be found stay None ("unknown"); nothing is guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobradar.parse.linkedin import linkedin_job_id

_LABELS = {
    "company": ("🏢", "company:", "employer:"),
    "location": ("📍", "location:"),
    "age": ("🕒", "⏰", "posted:"),
    "via": ("📌", "via:", "source:"),
}
_APPLY_TEXT = re.compile(r"apply\s*here|apply\s*now|^apply$", re.I)
_MARKUP = re.compile(r"[*_`~]+")
_ANNOUNCEMENT = re.compile(
    r"\b(new\s+batch|batch\s+of|jobs?\s+(just\s+)?(dropped|posted|added)|daily\s+digest|"
    r"subscribe|join\s+(our|the)\s+channel|share\s+(this|the)\s+channel|"
    r"\d+\s+new\s+jobs?)\b",
    re.I,
)
_REMOTE = re.compile(r"\bremote\b|\bwork\s+from\s+home\b|\bwfh\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_ONSITE = re.compile(r"\bon[-\s]?site\b|\bin[-\s]office\b", re.I)


@dataclass
class ParsedAlert:
    kind: str  # job | announcement | unparseable
    title: str | None = None
    company: str | None = None
    location: str | None = None
    reported_age: str | None = None  # e.g. "4 minutes ago" — source text, not a clock
    via: str | None = None
    destination_url: str | None = None
    linkedin_id: str | None = None
    remote_wording: str | None = None
    work_mode: str = "unknown"
    other_links: list[str] = field(default_factory=list)


def _clean(line: str) -> str:
    return _MARKUP.sub("", line).strip()


def _strip_label(line: str, labels: tuple[str, ...]) -> str | None:
    low = line.lower()
    for lab in labels:
        if low.startswith(lab.lower()):
            return line[len(lab):].strip(" :‍️") or None
    return None


def detect_work_mode(*texts: str | None) -> tuple[str, str | None]:
    joined = " ".join(t for t in texts if t)
    for mode, rx in (("hybrid", _HYBRID), ("remote", _REMOTE), ("onsite", _ONSITE)):
        m = rx.search(joined)
        if m:
            return mode, m.group(0)
    return "unknown", None


def parse_alert(text: str, links: list[dict]) -> ParsedAlert:
    """`links` are entity links: [{"text": "➡️ Apply Here", "url": "https://..."}]."""
    lines = [_clean(raw) for raw in text.splitlines()]
    lines = [ln for ln in lines if ln]
    urls = [lk["url"] for lk in links if lk.get("url")]

    if not lines:
        return ParsedAlert(kind="unparseable")

    fields: dict[str, str | None] = {k: None for k in _LABELS}
    title = None
    for ln in lines:
        matched = False
        for key, labels in _LABELS.items():
            val = _strip_label(ln, labels)
            if val is not None:
                fields[key] = fields[key] or val
                matched = True
                break
        if not matched and title is None and not _APPLY_TEXT.search(ln.lstrip("➡️ ")):
            title = ln

    # Destination: prefer the link whose anchor reads "Apply", else the first LinkedIn job URL.
    destination = None
    for lk in links:
        if lk.get("url") and _APPLY_TEXT.search(lk.get("text") or ""):
            destination = lk["url"]
            break
    if destination is None:
        destination = next((u for u in urls if linkedin_job_id(u)), None)

    has_fields = bool(fields["company"] or fields["location"])
    if destination is None and (not has_fields or _ANNOUNCEMENT.search(text)):
        return ParsedAlert(kind="announcement" if _ANNOUNCEMENT.search(text) else "unparseable",
                           title=title, other_links=urls)

    mode, wording = detect_work_mode(title, fields["location"])
    return ParsedAlert(
        kind="job",
        title=title,
        company=fields["company"],
        location=fields["location"],
        reported_age=fields["age"],
        via=fields["via"],
        destination_url=destination,
        linkedin_id=linkedin_job_id(destination) if destination else None,
        remote_wording=wording,
        work_mode=mode,
        other_links=[u for u in urls if u != destination],
    )
