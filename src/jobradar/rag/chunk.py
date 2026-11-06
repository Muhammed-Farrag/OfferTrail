"""Chunking for both collections.

* CV / notes → facts grouped under their section heading (one fact = one chunk), so every
  claim in an explanation can cite one reviewable fact.
* Job descriptions → paragraph chunks of at most ~700 characters.
"""

from __future__ import annotations

import re

_HEADING = re.compile(
    r"^\s*(?:#{1,4}\s+(?P<md>.+?)|(?P<caps>[A-Z][A-Z &/+-]{2,40})|(?P<colon>[A-Z][\w &/+-]{2,40}):)\s*$"
)
_BULLET = re.compile(r"^\s*(?:[-*•▪●◦]|\d+[.)])\s+")


def split_sections(text: str) -> list[tuple[str, str]]:
    """[(section_heading, body)] — the heading is '' before the first one."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m and len(line.strip()) <= 45:
            heading = (m.group("md") or m.group("caps") or m.group("colon") or "").strip()
            sections.append((heading.title() if heading.isupper() else heading, []))
        else:
            sections[-1][1].append(line)
    return [(h, "\n".join(b).strip()) for h, b in sections if "\n".join(b).strip()]


def facts_from_text(text: str) -> list[tuple[str, str]]:
    """[(section, fact_text)] — bullets become facts; prose paragraphs are split by sentence
    groups. Very short fragments (<12 chars) are dropped."""
    facts: list[tuple[str, str]] = []
    for section, body in split_sections(text):
        buf: list[str] = []

        def flush(section: str = section, buf: list[str] = buf) -> None:
            if buf:
                para = " ".join(s.strip() for s in buf).strip()
                for piece in _split_long(para, 400):
                    if len(piece) >= 12:
                        facts.append((section, piece))
                buf.clear()

        for line in body.splitlines():
            if not line.strip():
                flush()
            elif _BULLET.match(line):
                flush()
                buf.append(_BULLET.sub("", line))
                flush()
            else:
                buf.append(line)
        flush()
    return facts


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) + 1 > limit:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def chunk_description(text: str, limit: int = 700) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        for piece in _split_long(p, limit):
            if cur and len(cur) + len(piece) + 2 > limit:
                chunks.append(cur)
                cur = piece
            else:
                cur = f"{cur}\n\n{piece}".strip()
    if cur:
        chunks.append(cur)
    return chunks
