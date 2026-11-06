"""Source-channel reader using a user-authorized MTProto client (Telethon).

Why not the bot: Telegram delivers channel posts to bots only in channels where the bot is a
member, and we don't control the source channel. So a *user* session reads it. That session
is a full account credential (it could read any chat the account can), so it is stored under
data/ with 0600 permissions; application logic only ever resolves and reads the one
configured channel.

Coverage: first run backfills at most `backfill_max_messages` from the last `backfill_days`;
later runs catch up from the persisted cursor. Messages are stored before the cursor moves.
When a limit cuts coverage short, the gap is recorded in the run report (see /health).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable
from datetime import timedelta

from jobradar.context import AppContext
from jobradar.ingest.store import (
    RawMessage,
    advance_cursor,
    get_cursor,
    ingest_batch,
    store_message,
)
from jobradar.util import iso, utcnow

log = logging.getLogger(__name__)
FLOOD_WAIT_MAX_S = 900


def message_to_raw(msg, channel_id: str, username: str | None) -> RawMessage:
    """Convert a Telethon Message. Entity texts are taken via get_entities_text(), which
    handles Telegram's UTF-16 offsets."""
    from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

    links: list[dict] = []
    for ent, text in (msg.get_entities_text() or []):
        if isinstance(ent, MessageEntityTextUrl):
            links.append({"text": text, "url": ent.url})
        elif isinstance(ent, MessageEntityUrl):
            links.append({"text": text, "url": text})
    markup = getattr(msg, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for b in row.buttons:
            if getattr(b, "url", None):
                links.append({"text": b.text, "url": b.url})
    return RawMessage(channel_id=channel_id, message_id=msg.id, text=msg.message or "",
                      published_at=msg.date, links=links, edited_at=msg.edit_date,
                      channel_username=username)


async def collect(iter_messages: Callable[..., AsyncIterator], cursor: int | None,
                  max_messages: int, since_days: int) -> tuple[list, dict]:
    """Pure coverage logic over an async message iterator (newest first).

    `iter_messages(limit=..., min_id=...)` mirrors TelegramClient.iter_messages.
    """
    since = utcnow() - timedelta(days=since_days)
    got = []
    hit_days = False
    if cursor is None:
        async for m in iter_messages(limit=max_messages):
            if m.date < since:
                hit_days = True
                break
            got.append(m)
        mode = "backfill"
    else:
        async for m in iter_messages(limit=max_messages, min_id=cursor):
            got.append(m)
        mode = "catch_up"
    hit_limit = len(got) >= max_messages
    report = {"mode": mode, "fetched": len(got), "at": iso(), "max_messages": max_messages,
              "days": since_days, "hit_message_limit": hit_limit, "hit_day_limit": hit_days,
              "oldest_id": min((m.id for m in got), default=None),
              "newest_id": max((m.id for m in got), default=None)}
    if mode == "catch_up" and hit_limit:
        oldest = report["oldest_id"]
        report["complete"] = False
        report["gap"] = f"messages {cursor + 1}–{oldest - 1} were not fetched (limit " \
                        f"{max_messages}); they are skipped"
    elif mode == "backfill" and hit_limit:
        report["complete"] = False
        report["gap"] = f"backfill stopped at {max_messages} messages before covering " \
                        f"{since_days} days"
    else:
        report["complete"] = True
    return got, report


class ChannelReader:
    def __init__(self, ctx: AppContext, on_stored: Callable[[], object] | None = None) -> None:
        self.ctx = ctx
        self.on_stored = on_stored
        self.client = None
        self.entity = None
        self.channel_id: str | None = None

    def make_client(self):
        from telethon import TelegramClient

        s = self.ctx.settings
        if not (s.telegram_api_id and s.telegram_api_hash):
            raise RuntimeError("JOBRADAR_TELEGRAM_API_ID / _API_HASH are not set")
        client = TelegramClient(str(s.telegram_session_path), s.telegram_api_id,
                                s.telegram_api_hash.get_secret_value())
        client.flood_sleep_threshold = 60  # Telethon sleeps short flood waits automatically
        return client

    async def start(self) -> dict:
        from telethon import events, utils

        self.client = self.make_client()
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram reader is not authorized; run `jobradar auth-telegram`")
        session_file = self.ctx.settings.telegram_session_path.with_suffix(".session")
        if session_file.exists():
            os.chmod(session_file, 0o600)
        self.entity = await self._with_flood_retry(
            lambda: self.client.get_entity(self.ctx.settings.source_channel))
        self.channel_id = str(utils.get_peer_id(self.entity))
        report = await self.catch_up()
        self.client.add_event_handler(self._on_message, events.NewMessage(chats=self.entity))
        self.client.add_event_handler(self._on_message, events.MessageEdited(chats=self.entity))
        return report

    async def catch_up(self) -> dict:
        s = self.ctx.settings
        cursor = get_cursor(self.ctx.conn, self.channel_id)

        def iter_messages(**kw):
            return self.client.iter_messages(self.entity, **kw)

        msgs, report = await self._with_flood_retry(
            lambda: collect(iter_messages, cursor, s.backfill_max_messages, s.backfill_days))
        raws = [message_to_raw(m, self.channel_id, s.source_channel) for m in msgs]
        counts = ingest_batch(self.ctx.conn, raws, report)
        report["stored"] = counts
        log.info("telegram %s: %s", report["mode"], report)
        if self.on_stored:
            await _maybe_await(self.on_stored())
        return report

    async def _on_message(self, event) -> None:
        from telethon import utils

        # Defense in depth: only the configured channel is accepted.
        if str(utils.get_peer_id(event.message.peer_id)) != self.channel_id:
            return
        raw = message_to_raw(event.message, self.channel_id, self.ctx.settings.source_channel)
        store_message(self.ctx.conn, raw)  # persisted before the cursor moves
        advance_cursor(self.ctx.conn, self.channel_id, raw.message_id)  # never moves backward
        if self.on_stored:
            await _maybe_await(self.on_stored())

    async def _with_flood_retry(self, fn, attempts: int = 3):
        from telethon.errors import FloodWaitError

        for i in range(attempts):
            try:
                return await fn()
            except FloodWaitError as e:
                wait = min(e.seconds + 1, FLOOD_WAIT_MAX_S)
                log.warning("flood wait %ss (attempt %s/%s)", e.seconds, i + 1, attempts)
                if i == attempts - 1:
                    raise
                await asyncio.sleep(wait)

    async def run_until_disconnected(self):
        await self.client.run_until_disconnected()


async def _maybe_await(x):
    if asyncio.iscoroutine(x):
        await x


async def interactive_login(ctx: AppContext) -> None:
    """One-time phone + code login that creates the reader session file."""
    reader = ChannelReader(ctx)
    client = reader.make_client()
    await client.start()  # prompts for phone, code and 2FA password in the terminal
    me = await client.get_me()
    session_file = ctx.settings.telegram_session_path.with_suffix(".session")
    os.chmod(session_file, 0o600)
    print(f"Authorized as user id {me.id}. Session stored at {session_file} (mode 600).")
    entity = await client.get_entity(ctx.settings.source_channel)
    print(f"Source channel resolved: {getattr(entity, 'title', entity)}")
    await client.disconnect()
