"""Offline fixture/import mode: load channel messages and job descriptions from JSON.

Lets the whole workflow run without Telegram, Gmail or LLM credentials.
"""

from __future__ import annotations

import json
from pathlib import Path

from jobradar.ingest.store import RawMessage
from jobradar.util import parse_iso


def load_fixture(path: Path) -> tuple[list[RawMessage], dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    ch = data["channel"]
    messages = []
    for m in data["messages"]:
        messages.append(RawMessage(
            channel_id=str(ch["id"]),
            message_id=int(m["id"]),
            text=m["text"],
            published_at=parse_iso(m["date"]),
            links=m.get("links", []),
            edited_at=parse_iso(m["edited"]) if m.get("edited") else None,
            # synthetic posts don't exist in the real channel, so they get no permalink
            channel_username=None if m.get("synthetic") else ch.get("username"),
        ))
    return messages, data.get("descriptions", {})
