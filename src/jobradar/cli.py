"""Command-line entry point: `jobradar <command>`."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import re
import shutil
import sys
import tempfile
from pathlib import Path

from jobradar.config import Preferences, Settings, save_prefs

PRIVACY_NOTE = """What leaves this machine:
  • Embeddings run locally (fastembed) — CV text is NOT sent anywhere for indexing.
  • When an LLM is configured (Groq), these are sent to it: job description text (fact
    extraction), your approved facts relevant to a job plus its title (email drafting), and
    your question plus retrieved evidence chunks (chat). Your CV file itself is never sent.
  • Gmail receives only emails you explicitly approve.
Set JOBRADAR_LLM_BACKEND=none to keep everything local (rule-based analysis only)."""


def _plain(text: str) -> str:
    text = re.sub(r"<a href=\"([^\"]+)\">([^<]+)</a>", r"\2 <\1>", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _print_reply(reply, indent: str = "") -> None:
    for line in _plain(reply.text).splitlines():
        print(indent + line)
    for row in reply.buttons:
        print(indent + "  " + "  ".join(f"[{b.label}]" for b in row))


def _ctx(args):
    from jobradar.context import build_context

    return build_context(Settings())


# ---------------------------------------------------------------- commands
def cmd_init_db(args) -> None:
    ctx = _ctx(args)
    print(f"Database ready at {ctx.settings.db_path}")
    if not ctx.settings.prefs_path.exists():
        save_prefs(ctx.settings.prefs_path, Preferences())
        print(f"Created {ctx.settings.prefs_path} — every preference starts unset; "
              f"use `jobradar prefs set`.")


def cmd_import_fixture(args) -> None:
    from jobradar.ingest.fixtures import load_fixture
    from jobradar.ingest.store import ingest_batch
    from jobradar.pipeline import paste_description, process_pending_posts

    ctx = _ctx(args)
    messages, descriptions = load_fixture(Path(args.path))
    print("stored:", ingest_batch(ctx.conn, messages, {"mode": "fixture", "complete": True}))
    print("processed:", process_pending_posts(ctx))
    if args.with_descriptions:
        for key, text in descriptions.items():
            row = ctx.conn.execute("SELECT id FROM jobs WHERE canonical_key = ?", (key,)
                                   ).fetchone()
            if row:
                paste_description(ctx, row["id"], text)
        print(f"stored {len(descriptions)} fixture descriptions")


def cmd_onboard(args) -> None:
    from jobradar.candidate import (
        approve_cv,
        import_document,
        index_candidate,
        list_facts,
        review_facts,
    )
    from jobradar.pipeline import reanalyze_all

    ctx = _ctx(args)
    print(PRIVACY_NOTE, "\n")
    for path, kind in ((args.cv, "cv"), (args.notes, "notes")):
        if not path:
            continue
        res = import_document(ctx.conn, ctx.settings.documents_dir, Path(path), kind)
        print(f"{kind}: {path} → v{res.version}, extraction {res.status}, {res.fact_count} facts"
              + (" (already imported)" if res.duplicate else ""))
        if res.status != "ok":
            print("  ⚠️ No usable text. If this is a scanned PDF, provide a .txt/.md/.docx "
                  "version. The profile was not updated from this file.")
    pending = [f for f in list_facts(ctx.conn) if not f["approved"]]
    if pending:
        print("\nExtracted facts — only approved facts are ever used:")
        for f in pending:
            print(f"  [{f['id']}] ({f['section'] or '-'}) {f['text']}")
        if args.approve_all:
            approve = [f["id"] for f in pending]
        else:
            ans = input("\nApprove: 'all', comma-separated IDs, or blank for none: ").strip()
            approve = [f["id"] for f in pending] if ans == "all" else \
                [int(x) for x in ans.split(",") if x.strip().isdigit()]
        review_facts(ctx.conn, approve=approve)
        print(f"approved {len(approve)} facts")
    cv = ctx.conn.execute("SELECT id, version, approved_at, extraction_status FROM "
                          "candidate_documents WHERE kind='cv' ORDER BY version DESC LIMIT 1"
                          ).fetchone()
    if cv and not cv["approved_at"] and cv["extraction_status"] == "ok":
        if args.approve_all or input(f"Approve CV v{cv['version']} as the attachment for email "
                                     f"applications? [y/N] ").lower().startswith("y"):
            approve_cv(ctx.conn, cv["id"])
            print("CV approved")
    print(f"indexed {index_candidate(ctx.conn, ctx.embedder)} candidate chunks "
          f"({ctx.embedder.model_id})")
    print(f"re-analyzed {reanalyze_all(ctx)} jobs")
    missing = ctx.prefs.missing()
    if missing:
        print("\nPreferences still unset:", ", ".join(missing), "→ `jobradar prefs set KEY VALUE`")


def cmd_prefs(args) -> None:
    from jobradar.bot.service import BotService

    ctx = _ctx(args)
    svc = BotService(ctx)
    res = svc.cmd_settings([]) if args.action == "show" else svc.cmd_set([args.key, *args.value])
    for r in res.replies:
        _print_reply(r)


def cmd_jobs(args) -> None:
    from jobradar.rag.answer import list_jobs

    ctx = _ctx(args)
    decisions = ("uncertain",) if args.uncertain else ("candidate", "needs_review")
    for r in list_jobs(ctx, days=None if args.all else 7, decisions=decisions, limit=50):
        closed = " [closed]" if r["availability"] == "closed" else ""
        print(f"#{r['id']:<4} {r['rank_score'] or '-':>5}  {r['decision']:<12} "
              f"{(r['state'] or '-'):<18} {r['title']} — {r['publisher']}{closed}")


def cmd_card(args) -> None:
    from jobradar.bot.cards import render_job_card

    _print_reply(render_job_card(_ctx(args).conn, args.job_id))


def cmd_ask(args) -> None:
    from jobradar.rag.answer import answer

    ans = answer(_ctx(args), " ".join(args.question), args.job)
    print(ans.text)
    if ans.citations:
        print("citations:", ", ".join(ans.citations))


def cmd_paste(args) -> None:
    from jobradar.pipeline import paste_description

    print(json.dumps(paste_description(_ctx(args), args.job_id,
                                       Path(args.file).read_text(encoding="utf-8")), indent=2))


def cmd_health(args) -> None:
    from jobradar.health import format_health, health

    print(_plain(format_health(health(_ctx(args)))))


def cmd_auth_telegram(args) -> None:
    from jobradar.ingest.telegram_reader import interactive_login

    print("This signs a Telegram USER session in for reading the source channel. The session "
          "file is an account credential: keep data/ private and never commit it.")
    asyncio.run(interactive_login(_ctx(args)))


def cmd_auth_gmail(args) -> None:
    from jobradar.apply.gmail import run_oauth_flow

    s = Settings()
    s.ensure_data_dir()
    if not s.gmail_client_secret_path.exists():
        sys.exit(f"Put your OAuth desktop client JSON at {s.gmail_client_secret_path} first "
                 f"(see README → Gmail).")
    print("Requesting scope gmail.send only (send mail; cannot read your mailbox).")
    run_oauth_flow(s.gmail_client_secret_path, s.gmail_token_path)
    print(f"Token stored at {s.gmail_token_path} (mode 600).")


def cmd_run(args) -> None:
    from jobradar.apply.gmail import GmailApiSender
    from jobradar.bot.app import Runtime

    ctx = _ctx(args)
    s = ctx.settings
    if not s.bot_token:
        sys.exit("JOBRADAR_BOT_TOKEN is not set (see README → Bot).")
    if s.bot_allowed_user_id is None:
        print("⚠️ JOBRADAR_BOT_ALLOWED_USER_ID is not set: the bot runs in setup mode and only "
              "tells whoever messages it their user ID.")
    gmail_factory = (lambda: GmailApiSender(s.gmail_token_path)) \
        if s.gmail_token_path.exists() else None
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: "
                        "%(message)s")
    asyncio.run(Runtime(ctx, gmail_factory).run(with_reader=not args.no_reader))


def cmd_demo(args) -> None:
    from jobradar.demo import run_demo

    tmp = Path(tempfile.mkdtemp(prefix="jobradar-demo-"))
    try:
        run_demo(tmp)
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"\n(demo data kept at {tmp})")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="jobradar", description="AI Job Radar")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create the database and an empty preferences file")
    sp = sub.add_parser("import-fixture", help="offline import of channel messages from JSON")
    sp.add_argument("path")
    sp.add_argument("--with-descriptions", action="store_true")
    sp = sub.add_parser("onboard", help="import CV/notes and review extracted facts")
    sp.add_argument("--cv")
    sp.add_argument("--notes")
    sp.add_argument("--approve-all", action="store_true",
                    help="approve every extracted fact and the CV without prompting")
    sp = sub.add_parser("prefs", help="show or set preferences")
    sp.add_argument("action", choices=["show", "set"])
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="*")
    sp = sub.add_parser("jobs", help="list qualifying jobs")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--uncertain", action="store_true")
    sp = sub.add_parser("card", help="print one job card")
    sp.add_argument("job_id", type=int)
    sp = sub.add_parser("ask", help="ask a grounded question")
    sp.add_argument("question", nargs="+")
    sp.add_argument("--job", type=int)
    sp = sub.add_parser("paste", help="store a job description from a text file")
    sp.add_argument("job_id", type=int)
    sp.add_argument("file")
    sub.add_parser("health", help="health report")
    sub.add_parser("auth-telegram", help="authorize the channel reader (user session)")
    sub.add_parser("auth-gmail", help="authorize Gmail sending (gmail.send scope)")
    sp = sub.add_parser("run", help="run the bot + reader")
    sp.add_argument("--no-reader", action="store_true", help="bot only (no channel reading)")
    sp = sub.add_parser("demo", help="offline end-to-end demo (no credentials needed)")
    sp.add_argument("--keep", action="store_true", help="keep the temporary demo data")

    args = p.parse_args(argv)
    if args.cmd == "prefs" and args.action == "set" and not (args.key and args.value):
        p.error("prefs set needs KEY VALUE")
    {"init-db": cmd_init_db, "import-fixture": cmd_import_fixture, "onboard": cmd_onboard,
     "prefs": cmd_prefs, "jobs": cmd_jobs, "card": cmd_card, "ask": cmd_ask, "paste": cmd_paste,
     "health": cmd_health, "auth-telegram": cmd_auth_telegram, "auth-gmail": cmd_auth_gmail,
     "run": cmd_run, "demo": cmd_demo}[args.cmd](args)


if __name__ == "__main__":
    main()
