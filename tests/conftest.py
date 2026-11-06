from __future__ import annotations

import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

import jobradar
from jobradar.candidate import (
    approve_cv,
    import_document,
    index_candidate,
    list_facts,
    review_facts,
)
from jobradar.config import Preferences, Settings, save_prefs
from jobradar.context import AppContext
from jobradar.db.conn import open_db
from jobradar.ingest.fixtures import load_fixture
from jobradar.ingest.store import ingest_batch
from jobradar.llm.fake import FakeLLM
from jobradar.pipeline import paste_description, process_pending_posts
from jobradar.rag.embed import HashEmbedder
from jobradar.util import set_clock

FIXTURES = Path(jobradar.__file__).parent / "fixtures"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Automated tests never touch the network (and so never send real applications)."""
    def deny(*args, **kwargs):
        raise RuntimeError("network access is disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture(autouse=True)
def _frozen_clock():
    set_clock(NOW)
    yield
    set_clock(None)


def egypt_prefs(**overrides) -> Preferences:
    base = dict(full_name="Demo Candidate", residence_country="EG", citizenship=["EG"],
                include_internships=True, max_years_experience=2, languages=["ar", "en"],
                work_modes=["remote", "hybrid", "onsite"], open_to_relocation=True)
    base.update(overrides)
    return Preferences(**base)


@pytest.fixture
def ctx(tmp_path) -> AppContext:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", embed_backend="hash",
                        llm_backend="fake", gmail_sender="demo.candidate@example.com")
    settings.ensure_data_dir()
    prefs = egypt_prefs()
    save_prefs(settings.prefs_path, prefs)
    conn = open_db(settings.db_path)
    return AppContext(conn, settings, prefs, HashEmbedder(), FakeLLM())


def ingest_fixture(ctx: AppContext) -> dict[str, str]:
    messages, descriptions = load_fixture(FIXTURES / "channel_sample.json")
    ingest_batch(ctx.conn, messages)
    process_pending_posts(ctx)
    return descriptions


def onboard(ctx: AppContext) -> None:
    res = import_document(ctx.conn, ctx.settings.documents_dir, FIXTURES / "candidate_cv.md", "cv")
    assert res.status == "ok" and res.fact_count > 0
    review_facts(ctx.conn, approve=[f["id"] for f in list_facts(ctx.conn)])
    approve_cv(ctx.conn, res.document_id)
    index_candidate(ctx.conn, ctx.embedder)


def add_descriptions(ctx: AppContext, descriptions: dict[str, str]) -> None:
    for key, text in descriptions.items():
        job = ctx.conn.execute("SELECT id FROM jobs WHERE canonical_key = ?", (key,)).fetchone()
        if job:
            paste_description(ctx, job["id"], text)


def job_by_key(ctx: AppContext, key: str):
    return ctx.conn.execute("SELECT * FROM jobs WHERE canonical_key = ?", (key,)).fetchone()


def match_of(ctx: AppContext, job_id: int):
    return ctx.conn.execute(
        "SELECT * FROM match_results WHERE job_id = ? AND invalidated_at IS NULL", (job_id,)
    ).fetchone()


@pytest.fixture
def loaded(ctx) -> AppContext:
    """Fixture channel ingested, candidate onboarded, fixture descriptions stored."""
    onboard(ctx)
    descriptions = ingest_fixture(ctx)
    add_descriptions(ctx, descriptions)
    return ctx
