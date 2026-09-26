"""Entry point: Telegram bot wiring."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import httpx
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, ContextTypes, ExtBot, MessageHandler, TypeHandler, filters
from telegram.request import HTTPXRequest

import doctor
import health
import ingest
from answer import AnswerEngine
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

FILE_REJECTED_NON_ADMIN = (
    "Thanks, but only your manager can upload documents here. Please pass "
    "this file to them."
)

UNSUPPORTED_FILE_MESSAGE = (
    "I can only read .md, .txt, .docx, and .pdf files. Please convert this "
    "file and try again."
)


class HeartbeatBot(ExtBot):
    """ExtBot subclass that records every getUpdates call that returns
    successfully, empty or not, in PollingHealth. A call that raises records
    nothing. See health.py for why this alone isn't proof of a working bot."""

    def __init__(self, *args, polling_health: health.PollingHealth, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._polling_health = polling_health

    async def get_updates(self, *args, **kwargs):
        result = await super().get_updates(*args, **kwargs)
        self._polling_health.record_poll_success(result)
        return result


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


async def _answer_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, state: BotState, question: str) -> None:
    chat_id = update.effective_chat.id

    if len(state.corpus) == 0:
        await context.bot.send_message(chat_id=chat_id, text=NO_DOCUMENTS_MESSAGE)
        return

    async def _keep_typing() -> None:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)  # Telegram's typing indicator expires after ~5s

    typing_task = asyncio.create_task(_keep_typing())
    try:
        response = await asyncio.wait_for(state.answer_engine.answer(question), timeout=ANSWER_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        response = "I could not reach the language model. Please try again in a moment."
    finally:
        typing_task.cancel()

    await context.bot.send_message(chat_id=chat_id, text=response)


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
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(handle_error)

    logger.info("Deskmate starting for %s (%d documents loaded)", config.company_name, len(corpus))
    health.start_exit_watchdog(polling_health)
    application.run_polling(allowed_updates=Update.ALL_TYPES, timeout=health.POLL_TIMEOUT_SECONDS)


if __name__ == "__main__":
    main()
