"""Offline end-to-end demo: fixtures + fake LLM + hash embeddings + fake Gmail.

Walks through filtering, one closed listing, one email application (preview → approve →
send) and one manual application (open → I applied manually). Nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime
from pathlib import Path

from jobradar.apply.gmail import FakeGmail
from jobradar.bot.cards import Reply
from jobradar.bot.outbox import flush
from jobradar.bot.service import BotService
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
from jobradar.health import format_health, health
from jobradar.ingest.fixtures import load_fixture
from jobradar.ingest.store import ingest_batch
from jobradar.llm.fake import FakeLLM
from jobradar.pipeline import paste_description, process_pending_posts
from jobradar.rag.embed import HashEmbedder
from jobradar.util import set_clock

FIXTURES = Path(__file__).parent / "fixtures"
USER = 1  # demo Telegram user/chat ID


def _plain(text: str) -> str:
    text = re.sub(r"<a href=\"([^\"]+)\">([^<]+)</a>", r"\2", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def show(reply: Reply, indent: str = "  │ ") -> None:
    for line in _plain(reply.text).splitlines():
        print(indent + line)
    for row in reply.buttons:
        print(indent + "  " + "  ".join(f"[{b.label}]" for b in row))
    print()


class ConsoleTransport:
    def __init__(self) -> None:
        self.n = 0

    async def send(self, chat_id: int, reply: Reply) -> int:
        self.n += 1
        print(f"  ┌─ bot message #{self.n}")
        show(reply)
        return self.n

    async def edit(self, chat_id: int, message_id: int, reply: Reply) -> None:
        print(f"  ┌─ card #{message_id} updated in place")
        show(reply)


def banner(text: str) -> None:
    print("\n" + "═" * 78 + f"\n {text}\n" + "═" * 78)


def demo_prefs() -> Preferences:
    return Preferences(full_name="Demo Candidate", residence_country="EG", citizenship=["EG"],
                       include_internships=True, max_years_experience=2, languages=["ar", "en"],
                       work_modes=["remote", "hybrid", "onsite"],
                       require_sponsorship_abroad=True, open_to_relocation=True)


def build_demo_context(data_dir: Path) -> AppContext:
    settings = Settings(_env_file=None, data_dir=data_dir, embed_backend="hash",
                        llm_backend="fake", bot_allowed_user_id=USER,
                        gmail_sender="demo.candidate@example.com")
    settings.ensure_data_dir()
    prefs = demo_prefs()
    save_prefs(settings.prefs_path, prefs)
    return AppContext(open_db(settings.db_path), settings, prefs, HashEmbedder(), FakeLLM())


def run_demo(data_dir: Path) -> None:
    set_clock(datetime(2026, 11, 6, 12, 0, tzinfo=UTC))
    ctx = build_demo_context(data_dir)
    gmail = FakeGmail()
    svc = BotService(ctx, gmail_factory=lambda: gmail)
    transport = ConsoleTransport()
    conn = ctx.conn

    banner("1. Onboarding a SYNTHETIC CV (facts reviewed, CV approved)")
    res = import_document(conn, ctx.settings.documents_dir, FIXTURES / "candidate_cv.md", "cv")
    for f in list_facts(conn):
        print(f"  [{f['id']}] ({f['section']}) {f['text']}")
    review_facts(conn, approve=[f["id"] for f in list_facts(conn)])
    approve_cv(conn, res.document_id)
    print(f"  → {res.fact_count} facts approved; CV v{res.version} approved; "
          f"{index_candidate(conn, ctx.embedder)} chunks indexed")

    banner("2. Offline ingestion of fixture alerts (paraphrased + synthetic)")
    messages, descriptions = load_fixture(FIXTURES / "channel_sample.json")
    print("  stored:", ingest_batch(conn, messages, {"mode": "fixture", "complete": True}))
    print("  processed:", process_pending_posts(ctx))
    for key, text in descriptions.items():
        row = conn.execute("SELECT id FROM jobs WHERE canonical_key = ?", (key,)).fetchone()
        paste_description(ctx, row["id"], text)
    print(f"  stored {len(descriptions)} fixture job descriptions")
    print("\n  Decisions:")
    for r in conn.execute(
            "SELECT j.id, j.title, m.decision, m.reasons_json FROM jobs j JOIN match_results m "
            "ON m.job_id = j.id AND m.invalidated_at IS NULL ORDER BY j.id"):
        print(f"   #{r['id']:<3} {r['decision']:<13} {r['title']}  {r['reasons_json']}")

    banner("3. Notification outbox → one card per qualifying job")
    asyncio.run(flush(ctx, transport, USER, limit=50))

    def job_id(key: str) -> int:
        return conn.execute("SELECT id FROM jobs WHERE canonical_key = ?", (key,)).fetchone()[0]

    email_job = job_id("linkedin:9000000001")
    form_job = job_id("linkedin:9000000002")

    banner(f"4. EMAIL application walkthrough (#{email_job}, synthetic apply-by-email)")
    print("  User taps [Prepare email]:")
    out = svc.callback(USER, USER, f"prep:{email_job}")
    for r in out.replies:
        show(r)
    draft = conn.execute("SELECT id, content_hash FROM drafts WHERE superseded_at IS NULL AND "
                         "application_id = (SELECT id FROM applications WHERE job_id = ?)",
                         (email_job,)).fetchone()
    print("  User taps [Approve & send] (bound to the preview's content hash):")
    out = svc.callback(USER, USER, f"appr:{draft['id']}:{draft['content_hash'][:16]}")
    for r in out.replies:
        show(r)
    print("  A replayed tap of the same button:")
    again = svc.callback(USER, USER, f"appr:{draft['id']}:{draft['content_hash'][:16]}")
    for r in again.replies:
        show(r)
    print(f"  FakeGmail accepted {len(gmail.accepted)} message(s) in total.")
    asyncio.run(flush(ctx, transport, USER))

    banner(f"5. MANUAL application walkthrough (#{form_job}, employer form)")
    print("  User taps [Open application]:")
    out = svc.callback(USER, USER, f"open:{form_job}")
    for r in out.replies:
        show(r)
    state = conn.execute("SELECT state FROM applications WHERE job_id = ?", (form_job,)
                         ).fetchone()[0]
    print(f"  Application state after opening the link: {state} (not applied)")
    print("  User submits the form themselves, then taps [I applied manually]:")
    svc.callback(USER, USER, f"manual:{form_job}")
    asyncio.run(flush(ctx, transport, USER))

    banner("6. Grounded chat")
    for q in ("Show junior AI jobs from this week", f"Why does #{email_job} match me?",
              f"What is missing from my CV for #{form_job}?",
              "Which applications need manual work?"):
        print(f"  ❯ {q}")
        for r in svc.text(USER, USER, q).replies:
            show(r)

    banner("7. Health")
    print("\n".join("  " + line for line in _plain(format_health(health(ctx))).splitlines()))
    set_clock(None)
