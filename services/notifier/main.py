"""Notifier service.

Two roles:
1. Outbound: subscribe to Redis streams (signals, positions, marketfeed digests)
   and forward formatted alerts to the user's Telegram chat.
2. Inbound: long-poll Telegram for messages from the authorized user, forward
   them to the dashboard /api/chat endpoint, and reply with Opus's answer.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from shared.config import settings
from shared.redis_streams import subscribe

from formatter import (
    format_alert_triggered,
    format_live_watch_update,
    format_marketfeed_digest,
    format_position_event,
    format_signal_alert,
    format_system_alert,
)
from chat_client import chat_stream, render_progress, ChatProgress

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

STREAM_SIGNAL = "signal.recommendation"
STREAM_POSITION = "position.event"
STREAM_KNOWLEDGE = "knowledge.summary"
STREAM_ALERT = "alert.triggered"
STREAM_LIVE_WATCH = "live_watch.update"
GROUP = "notifier"

# Telegram message size hard cap is 4096 chars; leave headroom for markdown.
TELEGRAM_CHUNK = 3800


def _allowed_chat_id() -> int | None:
    if not settings.telegram_chat_id:
        return None
    try:
        return int(settings.telegram_chat_id)
    except (TypeError, ValueError):
        return None


async def _safe_send(bot: Bot, text: str, parse_mode: str | None = ParseMode.MARKDOWN) -> None:
    """Send a message, falling back to plain text if Markdown parsing fails."""
    try:
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode=parse_mode,
        )
    except Exception:
        logger.exception("Markdown send failed, retrying as plain text")
        try:
            await bot.send_message(chat_id=settings.telegram_chat_id, text=text)
        except Exception:
            logger.exception("Plain-text send also failed")


async def _send_chunked(bot: Bot, text: str) -> None:
    """Send a long message split into Telegram-sized chunks."""
    if not text:
        return
    chunks = [text[i : i + TELEGRAM_CHUNK] for i in range(0, len(text), TELEGRAM_CHUNK)]
    for chunk in chunks:
        await _safe_send(bot, chunk)


async def _consume_stream(
    stream: str,
    consumer_id: str,
    formatter,
    bot: Bot | None,
) -> None:
    """Run a single Redis consumer in a worker thread and forward to Telegram."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _reader() -> None:
        backoff = 1.0
        while True:
            try:
                for msg_id, data in subscribe(stream, group=GROUP, consumer=consumer_id, block=10_000):
                    asyncio.run_coroutine_threadsafe(queue.put((msg_id, data)), loop)
                    backoff = 1.0
            except Exception:
                logger.exception("Reader for %s crashed, retrying in %.1fs", stream, backoff)
                import time as _t
                _t.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    threading.Thread(target=_reader, daemon=True, name=f"reader-{stream}").start()

    while True:
        msg_id, data = await queue.get()
        logger.info("[%s] Received message %s", stream, msg_id)
        try:
            text = formatter(data)
            if not text:
                continue
            logger.info("[%s] Alert:\n%s", stream, text)
            if bot:
                await _safe_send(bot, text)
        except Exception:
            logger.exception("Failed to process/send %s message %s", stream, msg_id)


# ---------------------------------------------------------------------------
# Telegram inbound handlers
# ---------------------------------------------------------------------------

async def _handle_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward an incoming Telegram message to the dashboard chat endpoint and stream progress."""
    if update.effective_chat is None or update.message is None:
        return

    chat_id = update.effective_chat.id
    allowed = _allowed_chat_id()
    if allowed is not None and chat_id != allowed:
        await update.message.reply_text("Unauthorized.")
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    logger.info("Telegram chat in (%s): %r", chat_id, text[:120])

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    placeholder = None
    try:
        placeholder = await update.message.reply_text("\U0001f914 *Thinking…*", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        logger.exception("Failed to send placeholder")

    progress = ChatProgress()
    last_render = ""
    last_edit_at = 0.0

    async def _maybe_edit(force: bool = False) -> None:
        """Edit the placeholder, throttled to avoid Telegram rate limits."""
        nonlocal last_render, last_edit_at
        import time as _t

        if placeholder is None:
            return

        # Telegram allows ~1 edit/sec per message; throttle to be safe
        now = _t.time()
        if not force and (now - last_edit_at) < 1.2:
            return

        rendered = render_progress(progress)
        if rendered == last_render:
            return

        # Stay under Telegram's 4096 char limit
        if len(rendered) > TELEGRAM_CHUNK:
            rendered = rendered[: TELEGRAM_CHUNK - 20] + "\n…(truncated)"

        try:
            await placeholder.edit_text(rendered, parse_mode=ParseMode.MARKDOWN)
            last_render = rendered
            last_edit_at = now
        except Exception:
            # Markdown parse fail — try plain
            try:
                await placeholder.edit_text(rendered)
                last_render = rendered
                last_edit_at = now
            except Exception:
                pass  # rate limit or no change — ignore

    session_id = f"telegram_{chat_id}"

    try:
        async for event in chat_stream(text, session_id=session_id):
            if event.kind == "tool_call":
                progress.tool_calls.append(event.name or "?")
                await _maybe_edit()
            elif event.kind == "tool_result":
                if event.name and event.output is not None:
                    progress.tool_results[event.name] = event.output
                await _maybe_edit()
            elif event.kind == "token":
                progress.text += event.text or ""
                await _maybe_edit()
            elif event.kind == "error":
                progress.error = event.error
                progress.finished = True
                await _maybe_edit(force=True)
            elif event.kind == "done":
                progress.finished = True
                break
    except Exception as exc:
        logger.exception("chat_stream raised")
        progress.error = str(exc)
        progress.finished = True

    # Final render — force, even if throttled
    await _maybe_edit(force=True)

    # If the final answer overflows one Telegram message, append the overflow
    rendered = render_progress(progress)
    if len(rendered) > TELEGRAM_CHUNK:
        # The placeholder already has the truncated head; send the rest as follow-ups
        head_len = TELEGRAM_CHUNK - 20
        tail = rendered[head_len:]
        chunks = [tail[i : i + TELEGRAM_CHUNK] for i in range(0, len(tail), TELEGRAM_CHUNK)]
        for extra in chunks:
            await _safe_send(context.bot, extra)


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "Brent Trading Bot ready.\n\n"
        "Just send me any question and I'll research it live with the bot's data:\n"
        " • should I go long now?\n"
        " • show me my open positions\n"
        " • what's happening on @marketfeed?\n"
        " • simulate a long at 94.20 with SL 93.50 TP 95.50\n\n"
        "Commands:\n"
        " /state — current market snapshot\n"
        " /positions — open positions\n"
        " /help — this message"
    )


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_start(update, context)


async def _cmd_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shortcut: ask the chat backend for the current state."""
    fake_text = "Show me the current market state in a brief table."
    if update.message is None:
        return
    update.message.text = fake_text  # type: ignore[assignment]
    await _handle_chat_message(update, context)


async def _cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    fake_text = "List my open positions with live PnL."
    if update.message is None:
        return
    update.message.text = fake_text  # type: ignore[assignment]
    await _handle_chat_message(update, context)


# ---------------------------------------------------------------------------
# Live watch consumer (edits a single Telegram message in place per session)
# ---------------------------------------------------------------------------

async def _consume_live_watch(bot) -> None:
    """Special consumer that edits a single pinned Telegram message per watch session."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _reader() -> None:
        backoff = 1.0
        while True:
            try:
                for msg_id, data in subscribe(
                    STREAM_LIVE_WATCH, group=GROUP, consumer="notifier-livewatch", block=10_000
                ):
                    asyncio.run_coroutine_threadsafe(queue.put((msg_id, data)), loop)
                    backoff = 1.0
            except Exception:
                logger.exception("Live-watch reader crashed, retrying in %.1fs", backoff)
                import time as _t
                _t.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    threading.Thread(target=_reader, daemon=True, name="reader-live-watch").start()

    allowed_chat = _allowed_chat_id()
    if allowed_chat is None:
        logger.warning("No allowed chat — live watch disabled")
        return

    while True:
        msg_id, data = await queue.get()
        session_id = data.get("session_id")
        if not session_id:
            continue

        text = format_live_watch_update(data)
        if not text:
            continue
        if len(text) > TELEGRAM_CHUNK:
            text = text[:TELEGRAM_CHUNK - 20] + "\n\u2026(truncated)"

        # Read / set telegram_message_id via DB
        try:
            from shared.models.base import SessionLocal
            from shared.models.watch_sessions import WatchSession
        except Exception:
            logger.exception("Failed to import WatchSession")
            continue

        try:
            with SessionLocal() as session:
                row = session.get(WatchSession, session_id)
                existing_msg_id = row.telegram_message_id if row else None

            if existing_msg_id is None:
                # Send a new message and save its id
                sent = await bot.send_message(
                    chat_id=allowed_chat, text=text, parse_mode=ParseMode.MARKDOWN,
                )
                with SessionLocal() as session:
                    row = session.get(WatchSession, session_id)
                    if row:
                        row.telegram_chat_id = allowed_chat
                        row.telegram_message_id = sent.message_id
                        session.commit()
            else:
                try:
                    await bot.edit_message_text(
                        chat_id=allowed_chat,
                        message_id=existing_msg_id,
                        text=text,
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception as exc:
                    # Telegram rate limit / message too old — send a new one
                    err_str = str(exc)
                    if "message is not modified" in err_str:
                        pass  # no-op
                    elif "too old" in err_str.lower() or "bad request" in err_str.lower():
                        sent = await bot.send_message(
                            chat_id=allowed_chat, text=text, parse_mode=ParseMode.MARKDOWN,
                        )
                        with SessionLocal() as session:
                            row = session.get(WatchSession, session_id)
                            if row:
                                row.telegram_message_id = sent.message_id
                                session.commit()
                    else:
                        logger.exception("Edit failed")
        except Exception:
            logger.exception("live_watch dispatch failed for session %s", session_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async() -> None:
    if not settings.telegram_bot_token:
        logger.error("TELEGRAM_BOT_TOKEN is not set — notifier cannot run")
        return

    application: Application = (
        ApplicationBuilder().token(settings.telegram_bot_token).build()
    )

    # Inbound handlers
    application.add_handler(CommandHandler("start", _cmd_start))
    application.add_handler(CommandHandler("help", _cmd_help))
    application.add_handler(CommandHandler("state", _cmd_state))
    application.add_handler(CommandHandler("positions", _cmd_positions))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_chat_message))

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    bot = application.bot
    logger.info("Telegram Application started; polling for inbound messages")
    logger.info(
        "Notifier service starting — listening on streams: %s, %s, %s",
        STREAM_SIGNAL, STREAM_POSITION, STREAM_KNOWLEDGE,
    )

    try:
        await _safe_send(
            bot,
            format_system_alert(
                "Notifier started. Send me any question and I'll research it live."
            ),
        )
    except Exception:
        logger.exception("Failed to send startup message")

    try:
        await asyncio.gather(
            _consume_stream(STREAM_SIGNAL, "notifier-signal", format_signal_alert, bot),
            _consume_stream(STREAM_POSITION, "notifier-position", format_position_event, bot),
            _consume_stream(STREAM_KNOWLEDGE, "notifier-knowledge", format_marketfeed_digest, bot),
            _consume_stream(STREAM_ALERT, "notifier-alerts", format_alert_triggered, bot),
            _consume_live_watch(bot),
        )
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
