"""Small shared helpers: UTC timestamps, hashing, JSON."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

_clock_override: datetime | None = None


def utcnow() -> datetime:
    return _clock_override or datetime.now(UTC)


def set_clock(dt: datetime | None) -> None:
    """Freeze `utcnow()` (tests and the offline demo)."""
    global _clock_override
    _clock_override = dt


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def sha256_text(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x1f")  # unit separator so ("ab","c") != ("a","bc")
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def loads(s: str | None, default: Any = None) -> Any:
    if s is None:
        return default
    return json.loads(s)
