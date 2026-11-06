"""Wiring: one object that holds the connection, settings and pluggable backends."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from jobradar.config import Preferences, Settings, load_prefs
from jobradar.db.conn import open_db
from jobradar.llm.base import LLMProvider
from jobradar.rag.embed import Embedder, make_embedder


@dataclass
class AppContext:
    conn: sqlite3.Connection
    settings: Settings
    prefs: Preferences
    embedder: Embedder | None = None
    llm: LLMProvider | None = None
    llm_calls_this_cycle: int = 0
    fetch_transport: object | None = None  # httpx transport override for tests
    extras: dict = field(default_factory=dict)

    def reload_prefs(self) -> None:
        self.prefs = load_prefs(self.settings.prefs_path)

    def llm_allowed(self) -> bool:
        return self.llm is not None and self.llm_calls_this_cycle < \
            self.settings.llm_max_calls_per_cycle

    def new_cycle(self) -> None:
        self.llm_calls_this_cycle = 0


def build_context(settings: Settings | None = None) -> AppContext:
    settings = settings or Settings()
    settings.ensure_data_dir()
    conn = open_db(settings.db_path)
    embedder = make_embedder(settings.embed_backend, settings.embed_model,
                             str(settings.data_dir / "models"))
    llm: LLMProvider | None = None
    if settings.llm_backend == "groq" and settings.groq_api_key:
        from jobradar.llm.groq_provider import GroqProvider

        llm = GroqProvider(settings.groq_api_key.get_secret_value(), conn, settings.llm_model,
                           settings.llm_fallback_model, settings.llm_daily_token_budget)
    elif settings.llm_backend == "fake":
        from jobradar.llm.fake import FakeLLM

        llm = FakeLLM()
    return AppContext(conn, settings, load_prefs(settings.prefs_path), embedder, llm)
