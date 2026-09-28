"""Entry point: Telegram bot wiring."""

from __future__ import annotations

import asyncio
import logging
import secrets
import sys
import time
from pathlib import Path

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import Conflict, NetworkError
from telegram.request import BaseRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ExtBot,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

import doctor
import health
import ingest
import library_size
from answer import AnswerEngine, LibraryTooLarge, build_static_block
from config import Config, load_config
from corpus import Corpus, CorpusFull

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("deskmate.bot")

# httpx logs "HTTP Request: <method> <url> ..." at INFO, and the Telegram
# Bot API puts the bot token directly in the URL path
# (api.telegram.org/bot<TOKEN>/<method>). Capped here, not just via
# LOG_LEVEL, so raising LOG_LEVEL to DEBUG for troubleshooting can't
# reopen this leak — each logger's own level takes precedence over the
# root logger's.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class RedactSecretsFilter(logging.Filter):
    """Strip known secret values out of every log line before it's
    emitted, regardless of which logger or library produced it. This is a
    backstop behind the httpx/httpcore level caps above, in case some
    other library ever logs a secret at a level we don't expect."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = None
        return True


DEBOUNCE_SECONDS = 3
ANSWER_TIMEOUT_SECONDS = 65  # slightly above answer.API_TIMEOUT_SECONDS as a hard backstop
# While the library is too large (or empty) every question fails the same
# way; tell the admin once an hour per problem, not once per question.
LIBRARY_ALERT_INTERVAL_SECONDS = 3600
ALERT_LIBRARY_TOO_LARGE = "library_too_large"
ALERT_LIBRARY_EMPTY = "library_empty"
REMOVE_CONFIRM_SECONDS = 120
# How long a finished /remove request is remembered, so a repeat press on
# its buttons (double tap, or Telegram resending while a large file was
# being removed) is recognised instead of overwriting the result.
REMOVE_FINISHED_MEMORY_SECONDS = 600
REMOVE_CALLBACK_PREFIX = "rm"

GREETING = (
    "Hi, I'm {company_name}'s policy assistant. Ask me a question about "
    "company policy and I'll answer from our internal documents, with a "
    "source cited. If I can't find something in the documents, I'll say so "
    "rather than guess. Want a document added? Contact your manager."
)

NO_DOCUMENTS_MESSAGE = (
    "No documents have been uploaded yet, so I have nothing to answer from. "
    "Please contact your manager."
)

NO_DOCUMENTS_ADMIN_MESSAGE = (
    "Deskmate has no documents, so it can't answer staff questions. Send your "
    "policy documents to me in this chat as file attachments (.md, .txt, "
    ".docx, or .pdf)."
)

FILE_REJECTED_NON_ADMIN = (
    "Thanks, but only your manager can upload documents here. Please pass "
    "this file to them."
)

LIBRARY_TOO_LARGE_STAFF_MESSAGE = (
    "The document library is too large for the bot to read. Please ask your "
    "admin to remove some documents."
)

LIBRARY_TOO_LARGE_ADMIN_MESSAGE = (
    "Deskmate can't answer any questions right now: your documents are too "
    "large for the AI model to read in one go ({detail}). Remove some "
    "documents with /remove followed by the file name, e.g. "
    "/remove old-handbook.pdf. Send /docs to see the file names."
)

UNSUPPORTED_FILE_MESSAGE = (
    "I can only read .md, .txt, .docx, and .pdf files. Please convert this "
    "file and try again."
)


class HeartbeatBot(ExtBot):
    """ExtBot subclass that records every getUpdates call that returns
    successfully, empty or not, in PollingHealth. A call that raises records
    nothing. See health.py for why this alone isn't proof of a working bot.

    After a NetworkError (which includes Bad Gateway and TimedOut) it also
    throws away the getUpdates HTTP client, so PTB's retry opens a brand-new
    connection instead of reusing a keep-alive one that may be stuck to a bad
    Telegram frontend. A process restart, which also means a new connection,
    is what fixed the 2026-09-26 incident. Only the polling task uses this
    request object, one call at a time, so closing it here can't cut off a
    call in flight."""

    def __init__(
        self,
        *args,
        polling_health: health.PollingHealth,
        get_updates_request: BaseRequest,
        **kwargs,
    ) -> None:
        super().__init__(*args, get_updates_request=get_updates_request, **kwargs)
        self._polling_health = polling_health
        self._get_updates_request = get_updates_request

    async def get_updates(self, *args, **kwargs):
        try:
            result = await super().get_updates(*args, **kwargs)
        except NetworkError:
            await self._reset_get_updates_connection()
            raise
        self._polling_health.record_poll_success(result)
        return result

    async def _reset_get_updates_connection(self) -> None:
        try:
            await self._get_updates_request.shutdown()
            await self._get_updates_request.initialize()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not reset the getUpdates connection: %s", type(exc).__name__)
            return
        logger.info("Reset the getUpdates connection after a network error; the next poll opens a new one.")


def _telegram_request(connection_pool_size: int) -> HTTPXRequest:
    return HTTPXRequest(
        connection_pool_size=connection_pool_size,
        connect_timeout=health.TELEGRAM_CONNECT_TIMEOUT_SECONDS,
        read_timeout=health.TELEGRAM_READ_TIMEOUT_SECONDS,
        write_timeout=health.TELEGRAM_WRITE_TIMEOUT_SECONDS,
        pool_timeout=health.TELEGRAM_POOL_TIMEOUT_SECONDS,
    )


class BotState:
    def __init__(self, config: Config, corpus: Corpus) -> None:
        self.config = config
        self.corpus = corpus
        self.answer_engine = AnswerEngine(config, corpus)
        self.pending_buffers: dict[tuple[int, int], dict] = {}
        # When each kind of admin alert was last sent (monotonic seconds).
        self.admin_alert_sent_at: dict[str, float] = {}
        # /remove confirmations waiting for a button press, keyed by a short
        # random token (Telegram caps button data at 64 bytes, too short for
        # some file names). In memory only: a restart drops them, and the
        # admin just sends /remove again.
        self.pending_removals: dict[str, dict] = {}
        self.finished_removals: dict[str, float] = {}


def _is_allowed_chat(state: BotState, chat_id: int) -> bool:
    if not state.config.allowed_chat_ids:
        return True
    return chat_id in state.config.allowed_chat_ids


def _is_admin(state: BotState, user_id: int) -> bool:
    return user_id == state.config.admin_user_id


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.bot_data["state"]
    await update.message.reply_text(GREETING.format(company_name=state.config.company_name))


async def handle_docs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.bot_data["state"]
    if len(state.corpus) == 0:
        if _is_admin(state, update.effective_user.id):
            await update.message.reply_text(NO_DOCUMENTS_ADMIN_MESSAGE)
        else:
            await update.message.reply_text(NO_DOCUMENTS_MESSAGE)
        return

    lines = ["Documents I can answer from:"]
    for doc in state.corpus.documents:
        date = doc.ingested_at.split("T")[0]
        lines.append(f"- {doc.filename} (added {date})")
    await update.message.reply_text("\n".join(lines))


ADMIN_ONLY_MESSAGE = "Only your manager can use this command."


async def handle_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.bot_data["state"]
    if not _is_admin(state, update.effective_user.id):
        await update.message.reply_text(ADMIN_ONLY_MESSAGE)
        return
    report = doctor.run_doctor(state.config, state.corpus)
    await update.message.reply_text(report)


async def handle_reset_demo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.bot_data["state"]
    if not _is_admin(state, update.effective_user.id):
        await update.message.reply_text(ADMIN_ONLY_MESSAGE)
        return
    removed = state.corpus.reset_demo()
    if not removed:
        await update.message.reply_text("No demo documents to remove — the demo corpus is already gone.")
        return
    await update.message.reply_text(
        "Removed demo documents: " + ", ".join(removed) + ". Upload your real documents when ready."
    )


REMOVE_USAGE_MESSAGE = (
    "Send /remove followed by the file name, e.g. /remove old-handbook.pdf. "
    "Send /docs to see the file names."
)


async def handle_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.bot_data["state"]
    if not _is_admin(state, update.effective_user.id):
        await update.message.reply_text(ADMIN_ONLY_MESSAGE)
        return

    # The whole rest of the message is the name, so names with spaces work.
    parts = (update.message.text or "").split(maxsplit=1)
    name = parts[1].strip() if len(parts) > 1 else ""
    if not name:
        await update.message.reply_text(REMOVE_USAGE_MESSAGE)
        return

    matches = state.corpus.find(name)
    if not matches:
        await update.message.reply_text(
            f"I couldn't find a document called {name}. Send /docs to see the exact file names."
        )
        return
    if len(matches) > 1:
        await update.message.reply_text(
            "More than one document matches that name: " + ", ".join(matches)
            + ". Send /remove with the exact name, including capital letters."
        )
        return

    filename = matches[0]
    doc = state.corpus.get(filename)
    now = time.monotonic()
    for token, pending in list(state.pending_removals.items()):
        if pending["expires_at"] <= now:
            del state.pending_removals[token]
    token = secrets.token_hex(4)
    state.pending_removals[token] = {"filename": filename, "expires_at": now + REMOVE_CONFIRM_SECONDS}

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Remove", callback_data=f"{REMOVE_CALLBACK_PREFIX}:{token}:yes"),
            InlineKeyboardButton("Cancel", callback_data=f"{REMOVE_CALLBACK_PREFIX}:{token}:no"),
        ]]
    )
    await update.message.reply_text(
        f"Remove {filename} ({doc.word_count:,} words, added {doc.ingested_at.split('T')[0]})? "
        "The bot will stop using it for answers. This request expires in 2 minutes.",
        reply_markup=keyboard,
    )


async def handle_remove_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.bot_data["state"]
    query = update.callback_query

    # Buttons are visible to everyone in a group; only the admin's press counts.
    if not _is_admin(state, query.from_user.id):
        await query.answer(ADMIN_ONLY_MESSAGE, show_alert=True)
        return

    _, token, choice = (query.data or "::").split(":", 2)
    now = time.monotonic()
    for old_token, finished_at in list(state.finished_removals.items()):
        if now - finished_at > REMOVE_FINISHED_MEMORY_SECONDS:
            del state.finished_removals[old_token]
    if token in state.finished_removals:
        logger.info("Ignored a repeat press on a finished /remove request")
        await query.answer("Already done.")
        return

    pending = state.pending_removals.pop(token, None)
    if pending is None or pending["expires_at"] <= now:
        await query.answer()
        await query.edit_message_text("This request has expired. Send /remove again.")
        return

    # Mark finished before any await: a repeat press queued behind this one
    # must see it, whatever happens next.
    state.finished_removals[token] = now
    filename = pending["filename"]
    await query.answer()
    if choice != "yes":
        logger.info("/remove of %s cancelled", filename)
        await query.edit_message_text(f"Cancelled. {filename} was not removed.")
        return
    logger.info("/remove of %s confirmed", filename)

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, state.corpus.remove_document, filename)
    except KeyError:
        await query.edit_message_text(f"{filename} was already removed.")
        return
    except Exception as exc:  # noqa: BLE001
        logger.error("Removing %s failed: %s", filename, exc)
        doctor.record_last_error(f"Removing {filename} failed: {exc}")
        await query.edit_message_text(f"I couldn't remove {filename}. Please try again.")
        return

    remaining = len(state.corpus)
    message = f"Removed {filename}. {remaining} document{'s' if remaining != 1 else ''} left."
    if remaining == 0:
        message += " The bot can't answer questions until you upload documents."
    await query.edit_message_text(message)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.bot_data["state"]
    user_id = update.effective_user.id

    if not _is_admin(state, user_id):
        await update.message.reply_text(FILE_REJECTED_NON_ADMIN)
        return

    document = update.message.document
    filename = document.file_name
    suffix = Path(filename).suffix.lower()
    if suffix not in ingest.SUPPORTED_EXTENSIONS:
        await update.message.reply_text(UNSUPPORTED_FILE_MESSAGE)
        return

    telegram_file = await context.bot.get_file(document.file_id)
    raw_bytes = bytes(await telegram_file.download_as_bytearray())

    try:
        loop = asyncio.get_running_loop()
        doc = await asyncio.wait_for(
            loop.run_in_executor(None, state.corpus.add_document, filename, raw_bytes),
            timeout=30,
        )
    except CorpusFull as exc:
        await update.message.reply_text(str(exc))
        return
    except (ingest.FileTooLarge, ingest.UnsupportedFileType) as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        logger.error("Ingestion failed for %s: %s", filename, exc)
        doctor.record_last_error(f"Ingestion failed for {filename}: {exc}")
        await update.message.reply_text(
            f"I couldn't process {filename}. Please check the file and try again."
        )
        return

    await update.message.reply_text(
        f"Got it: {doc.filename} ingested, {doc.word_count:,} words extracted."
    )
    await _send_library_size_report(update, state)


LIBRARY_SIZE_TIMEOUT_SECONDS = 30


async def _send_library_size_report(update: Update, state: BotState) -> None:
    """Follow-up to a successful upload: total library size, estimated cost
    per question, and a warning when either gets high. Informational only;
    any failure here is logged and the upload stands."""
    try:
        static_block = build_static_block(state.config, state.corpus)
        loop = asyncio.get_running_loop()
        size = await asyncio.wait_for(
            loop.run_in_executor(
                None, library_size.measure, state.answer_engine.client, state.config.model, static_block
            ),
            timeout=LIBRARY_SIZE_TIMEOUT_SECONDS,
        )
        report = library_size.format_report(size, state.config.model, len(state.corpus))
        logger.info(
            "Library size after upload: %d tokens (%s), %.0f%% of %d-token context window",
            size.tokens,
            "counted" if size.tokens_exact else "estimated",
            size.context_fraction * 100,
            size.context_tokens,
        )
        await update.message.reply_text(report)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not send the library size report: %s", exc)


async def _answer_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, state: BotState, question: str) -> None:
    chat_id = update.effective_chat.id

    if len(state.corpus) == 0:
        if chat_id == state.config.admin_user_id:
            await context.bot.send_message(chat_id=chat_id, text=NO_DOCUMENTS_ADMIN_MESSAGE)
            state.admin_alert_sent_at[ALERT_LIBRARY_EMPTY] = time.monotonic()
            return
        await context.bot.send_message(chat_id=chat_id, text=NO_DOCUMENTS_MESSAGE)
        await _alert_admin(context, state, ALERT_LIBRARY_EMPTY, NO_DOCUMENTS_ADMIN_MESSAGE)
        return

    async def _keep_typing() -> None:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)  # Telegram's typing indicator expires after ~5s

    typing_task = asyncio.create_task(_keep_typing())
    library_too_large: LibraryTooLarge | None = None
    try:
        response = await asyncio.wait_for(state.answer_engine.answer(question), timeout=ANSWER_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        response = "I could not reach the language model. Please try again in a moment."
    except LibraryTooLarge as exc:
        library_too_large = exc
        response = LIBRARY_TOO_LARGE_STAFF_MESSAGE
    finally:
        typing_task.cancel()

    if library_too_large is not None and chat_id == state.config.admin_user_id:
        # The admin asked in their own DM: give them the real cause directly
        # instead of a message telling them to ask themselves.
        await context.bot.send_message(
            chat_id=chat_id, text=LIBRARY_TOO_LARGE_ADMIN_MESSAGE.format(detail=library_too_large.detail)
        )
        state.admin_alert_sent_at[ALERT_LIBRARY_TOO_LARGE] = time.monotonic()
        return

    await context.bot.send_message(chat_id=chat_id, text=response)
    if library_too_large is not None:
        await _alert_admin(
            context,
            state,
            ALERT_LIBRARY_TOO_LARGE,
            LIBRARY_TOO_LARGE_ADMIN_MESSAGE.format(detail=library_too_large.detail),
        )


async def _alert_admin(context: ContextTypes.DEFAULT_TYPE, state: BotState, kind: str, text: str) -> None:
    """DM the admin about a problem staff just hit, at most once an hour per
    kind of problem. A failure here is logged, never raised: the staff reply
    has already gone out."""
    now = time.monotonic()
    last_sent = state.admin_alert_sent_at.get(kind)
    if last_sent is not None and now - last_sent < LIBRARY_ALERT_INTERVAL_SECONDS:
        return
    state.admin_alert_sent_at[kind] = now
    try:
        await context.bot.send_message(chat_id=state.config.admin_user_id, text=text)
    except Exception as send_exc:  # noqa: BLE001
        logger.warning("Could not send the admin a %s alert: %s", kind, send_exc)


async def _flush_after_delay(key: tuple[int, int], context: ContextTypes.DEFAULT_TYPE, state: BotState) -> None:
    await asyncio.sleep(DEBOUNCE_SECONDS)
    # pop(), not read-then-delete: guarantees a message arriving exactly at
    # flush time can't be picked up by two coroutines at once.
    buffer = state.pending_buffers.pop(key, None)
    if buffer is None:
        return
    await _answer_and_reply(buffer["update"], context, state, buffer["text"])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.bot_data["state"]

    if update.message is None or not update.message.text:
        return

    chat_id = update.effective_chat.id
    if not _is_allowed_chat(state, chat_id):
        return

    # A Telegram client sometimes splits one long paste into several
    # consecutive messages. Merge them within the debounce window instead
    # of answering each fragment separately.
    key = (chat_id, update.effective_user.id)
    buffer = state.pending_buffers.get(key)
    if buffer is not None:
        buffer["timer_task"].cancel()
        buffer["text"] += "\n" + update.message.text
        buffer["update"] = update
    else:
        buffer = {"text": update.message.text, "update": update}
        state.pending_buffers[key] = buffer

    buffer["timer_task"] = asyncio.create_task(_flush_after_delay(key, context, state))


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registered with Application.add_error_handler so PTB has somewhere
    to send errors instead of dumping a full traceback via "No error
    handlers are registered". Without this, every Railway redeploy — old
    and new container briefly polling the same bot token at once — logs a
    scary traceback for what is actually an expected, self-recovering
    condition (PTB's polling loop already retries indefinitely on its
    own; registering a handler doesn't change that, it only controls how
    the error gets reported)."""
    error = context.error

    if isinstance(error, Conflict):
        logger.warning(
            "Another Deskmate instance is polling for the same bot (expected "
            "briefly during a Railway redeploy) — Telegram rejected this "
            "instance's poll; retrying."
        )
        return

    logger.error("Unhandled error while processing update %s: %s", update, error, exc_info=error)
    doctor.record_last_error(f"Unhandled error: {error}")


def main() -> None:
    config = load_config()
    logging.getLogger().setLevel(config.log_level)

    redact_filter = RedactSecretsFilter(
        [config.telegram_bot_token, config.anthropic_api_key, config.healthcheck_ping_url or ""]
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact_filter)

    corpus = Corpus.load()
    state = BotState(config, corpus)

    polling_health = health.PollingHealth()
    bot = HeartbeatBot(
        token=config.telegram_bot_token,
        base_url=config.telegram_api_base_url,
        request=_telegram_request(connection_pool_size=256),
        get_updates_request=_telegram_request(connection_pool_size=1),
        polling_health=polling_health,
    )
    background_tasks: list[asyncio.Task] = []
    heartbeat_client: httpx.AsyncClient | None = None

    async def _start_monitoring(application: Application) -> None:
        nonlocal heartbeat_client
        background_tasks.append(
            asyncio.create_task(health.queue_monitor_loop(polling_health, application.update_queue))
        )
        background_tasks.append(asyncio.create_task(health.delivery_check_loop(polling_health, application.bot)))
        if config.healthcheck_ping_url:
            heartbeat_client = httpx.AsyncClient()
            background_tasks.append(
                asyncio.create_task(
                    health.heartbeat_loop(polling_health, heartbeat_client, config.healthcheck_ping_url)
                )
            )

    async def _stop_monitoring(application: Application) -> None:
        # Cancel and await inside the running loop, so the heartbeat's httpx
        # client closes here instead of during garbage collection after the
        # loop is gone (which raised sniffio.AsyncLibraryNotFoundError).
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        if heartbeat_client is not None:
            await heartbeat_client.aclose()

    builder = Application.builder().bot(bot).post_init(_start_monitoring).post_shutdown(_stop_monitoring)
    application = builder.build()
    application.bot_data["state"] = state

    async def _mark_update_processed(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        polling_health.record_update_processed()

    # Group -1 runs before the real handlers for every update and doesn't
    # stop them; it only tells the queue-stall check that processing moves.
    application.add_handler(TypeHandler(object, _mark_update_processed), group=-1)
    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(CommandHandler("docs", handle_docs))
    application.add_handler(CommandHandler("doctor", handle_doctor))
    application.add_handler(CommandHandler("reset_demo", handle_reset_demo))
    application.add_handler(CommandHandler("remove", handle_remove))
    application.add_handler(CallbackQueryHandler(handle_remove_button, pattern=rf"^{REMOVE_CALLBACK_PREFIX}:"))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(handle_error)

    logger.info("Deskmate starting for %s (%d documents loaded)", config.company_name, len(corpus))
    health.start_exit_watchdog(polling_health)
    application.run_polling(allowed_updates=Update.ALL_TYPES, timeout=health.POLL_TIMEOUT_SECONDS)


if __name__ == "__main__":
    main()
