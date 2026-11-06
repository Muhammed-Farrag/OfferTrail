"""python-telegram-bot adapter and the long-running process (`jobradar run`).

One asyncio loop runs: the private bot (long polling), the Telethon channel reader, and a
periodic cycle (process stored posts → flush the notification outbox → digest). The process
must be running to monitor; after sleep/downtime the reader catches up from its cursor.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from jobradar.apply.sender import recover_stale_claims
from jobradar.bot.cards import Reply
from jobradar.bot.outbox import flush, send_digest
from jobradar.bot.service import BotService, Result
from jobradar.candidate import import_document
from jobradar.context import AppContext
from jobradar.pipeline import enrich_pending, mark_card_dirty, process_pending_posts

log = logging.getLogger(__name__)
COMMANDS = ["start", "help", "jobs", "job", "pending", "applied", "profile", "approvefacts",
            "rejectfact", "approvecv", "settings", "set", "editdraft", "pause", "resume",
            "health"]
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
CYCLE_SECONDS = 60
DIGEST_SECONDS = 6 * 3600


def _markup(reply: Reply):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if not reply.buttons:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(b.label, url=b.url) if b.url else
         InlineKeyboardButton(b.label, callback_data=b.callback) for b in row]
        for row in reply.buttons])


class PTBTransport:
    def __init__(self, bot) -> None:
        self.bot = bot

    async def send(self, chat_id: int, reply: Reply) -> int:
        from telegram import LinkPreviewOptions
        from telegram.constants import ParseMode

        msg = await self.bot.send_message(chat_id, reply.text[:4096], parse_mode=ParseMode.HTML,
                                          reply_markup=_markup(reply),
                                          link_preview_options=LinkPreviewOptions(is_disabled=True))
        return msg.message_id

    async def edit(self, chat_id: int, message_id: int, reply: Reply) -> None:
        from telegram import LinkPreviewOptions
        from telegram.constants import ParseMode

        await self.bot.edit_message_text(reply.text[:4096], chat_id=chat_id,
                                         message_id=message_id, parse_mode=ParseMode.HTML,
                                         reply_markup=_markup(reply),
                                         link_preview_options=LinkPreviewOptions(is_disabled=True))


class Runtime:
    def __init__(self, ctx: AppContext, gmail_factory=None) -> None:
        self.ctx = ctx
        self.service = BotService(ctx, gmail_factory)
        s = ctx.settings
        self.chat_id = s.bot_allowed_chat_id or s.bot_allowed_user_id
        self.transport: PTBTransport | None = None
        self._lock = asyncio.Lock()

    async def cycle(self) -> None:
        async with self._lock:
            self.ctx.new_cycle()
            counts = process_pending_posts(self.ctx)
            if any(counts.values()):
                log.info("processed posts: %s", counts)
            enrich_pending(self.ctx)
            if self.transport and self.chat_id:
                await flush(self.ctx, self.transport, self.chat_id)

    async def deliver(self, update, result: Result) -> None:
        for r in result.replies:
            await self.transport.send(update.effective_chat.id, r)
        if result.refresh_job is not None:
            mark_card_dirty(self.ctx.conn, result.refresh_job)
            await flush(self.ctx, self.transport, self.chat_id)

    # ---------------------------------------------------------------- PTB handlers
    async def on_command(self, update, context) -> None:
        name = update.message.text.split()[0][1:].split("@")[0].lower()
        res = self.service.command(update.effective_user.id, update.effective_chat.id, name,
                                   list(context.args or []))
        await self.deliver(update, res)

    async def on_text(self, update, context) -> None:
        res = self.service.text(update.effective_user.id, update.effective_chat.id,
                                update.message.text)
        await self.deliver(update, res)

    async def on_callback(self, update, context) -> None:
        q = update.callback_query
        res = self.service.callback(update.effective_user.id, update.effective_chat.id,
                                    q.data or "")
        await q.answer(res.toast[:190] if res.toast else None)
        await self.deliver(update, res)

    async def on_document(self, update, context) -> None:
        uid, cid = update.effective_user.id, update.effective_chat.id
        if not self.service.authorized(uid, cid):
            await self.deliver(update, Result([self.service.unauthorized_reply(uid)]))
            return
        doc = update.message.document
        if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
            await self.deliver(update, Result([Reply("File too large (max 10 MB).")]))
            return
        suffix = Path(doc.file_name or "cv.txt").suffix.lower()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"upload{suffix}"
            f = await doc.get_file()
            await f.download_to_drive(path)
            path = path.rename(Path(tmp) / (doc.file_name or path.name))
            try:
                res = import_document(self.ctx.conn, self.ctx.settings.documents_dir, path, "cv")
            except Exception as ex:
                await self.deliver(update, Result([Reply(f"Could not import: {ex}")]))
                return
        if res.status != "ok":
            text = (f"⚠️ Text extraction {res.status} for {doc.file_name}. If it is a scanned "
                    f"PDF, send a TXT/MD/DOCX version. The profile was NOT updated.")
        else:
            text = (f"Imported {doc.file_name} as CV v{res.version} with {res.fact_count} "
                    f"extracted facts{' (already imported)' if res.duplicate else ''}. Review "
                    f"them with /profile, then /approvefacts and /approvecv.")
        await self.deliver(update, Result([Reply(text)]))

    def build_application(self):
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            filters,
        )

        token = self.ctx.settings.bot_token.get_secret_value()
        app = Application.builder().token(token).build()
        app.add_handler(CommandHandler(COMMANDS, self.on_command))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        self.transport = PTBTransport(app.bot)
        return app

    async def run(self, with_reader: bool = True) -> None:
        n = recover_stale_claims(self.ctx.conn)
        if n:
            log.warning("%s send attempts were interrupted; marked send_unknown", n)
        app = self.build_application()
        async with app:
            await app.start()
            await app.updater.start_polling(drop_pending_updates=False)
            reader = None
            if with_reader:
                from jobradar.ingest.telegram_reader import ChannelReader

                reader = ChannelReader(self.ctx, on_stored=self.cycle)
                report = await reader.start()
                log.info("reader ready: %s", report)
            await self.cycle()
            ticker = asyncio.create_task(self._ticker())
            try:
                if reader:
                    await reader.run_until_disconnected()
                else:
                    await asyncio.Event().wait()
            finally:
                ticker.cancel()
                await app.updater.stop()
                await app.stop()

    async def _ticker(self) -> None:
        elapsed = 0
        while True:
            await asyncio.sleep(CYCLE_SECONDS)
            elapsed += CYCLE_SECONDS
            try:
                await self.cycle()
                if self.ctx.prefs.notify_mode == "digest" and elapsed >= DIGEST_SECONDS:
                    elapsed = 0
                    await send_digest(self.ctx, self.transport, self.chat_id)
            except Exception:
                log.exception("cycle failed")
