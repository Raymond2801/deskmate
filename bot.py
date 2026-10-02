"""Entry point: Telegram bot wiring."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    Update,
)
from telegram.constants import ChatType
from telegram.constants import ChatAction
from telegram.error import Conflict, NetworkError
from telegram.request import BaseRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
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
import licensing
from answer import API_UNREACHABLE_MESSAGE, AnswerEngine, LibraryTooLarge, PreviousExchange, build_static_block
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
REMEMBERED_MESSAGES = 500
ANSWER_FIRST_LINE_PREFIX = "Answer:"
REMOVE_CALLBACK_PREFIX = "rm"

GREETING = (
    "Hi, I'm {company_name}'s policy assistant. Ask me a question about "
    "company policy and I'll answer from our internal documents, with a "
    "source cited. If I can't find something in the documents, I'll say so "
    "rather than guess. Want a document added? Contact your manager.\n\n"
    "In a group chat, tap / and choose /ask, then reply to my question with "
    "yours. To ask a follow-up, reply to one of my answers. I only read "
    "messages sent directly to me."
)

# Only reachable when group privacy is off (Telegram doesn't deliver
# mentions to a bot with privacy on), so it points at /ask instead.
GROUP_EMPTY_QUESTION_MESSAGE = "Tap / and choose /ask, then reply to my question with yours."

# ForceReply doesn't open the reply box on its own in every Telegram app
# (not on Telegram Web or the phone app we tested), and with group privacy
# on a plain message never reaches the bot, so say how to reply.
ASK_PROMPT_MESSAGE = "What's your question? Reply to this message (tap and hold → Reply) and type it."

STAFF_COMMANDS = [
    BotCommand("ask", "Ask a policy question"),
    BotCommand("docs", "List the documents I answer from"),
]
ADMIN_COMMANDS = STAFF_COMMANDS + [
    BotCommand("remove", "Remove a document"),
    BotCommand("reset_demo", "Remove the demo documents"),
    BotCommand("doctor", "Diagnostic report"),
]

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

LICENSE_LOCKED_STAFF_MESSAGE = "Deskmate is not available right now. Please contact your administrator."

# What the admin is told while the bot is locked, by lock reason. Sent when
# the bot locks, and as the reply to anything the admin sends while locked.
LICENSE_LOCKED_ADMIN_MESSAGES = {
    licensing.REASON_MISSING: (
        "Deskmate is locked because no license key is set, so it is not answering anyone. "
        "Add your Gumroad license key as the LICENSE_KEY variable in Railway, then redeploy."
    ),
    licensing.REASON_INVALID: (
        "Deskmate is locked because Gumroad does not recognise the license key in LICENSE_KEY, "
        "so it is not answering anyone. Check the key against your Gumroad receipt, fix LICENSE_KEY "
        "in Railway and redeploy. Send /license to check again."
    ),
    licensing.REASON_REVOKED: (
        "Deskmate is locked because the purchase for this license key was refunded or disputed, "
        "so it is not answering anyone. If you think this is a mistake, contact the seller, then "
        "send /license to check again."
    ),
    licensing.REASON_ACTIVATION_LIMIT: (
        f"Deskmate is locked because this license key has been activated more than "
        f"{licensing.MAX_ACTIVATIONS} times, so it is not answering anyone. Each purchase covers up "
        f"to {licensing.MAX_ACTIVATIONS} activations. Ask the seller to reset the count, then send "
        "/license to check again."
    ),
    licensing.REASON_UNVERIFIED: (
        "Deskmate could not reach Gumroad to check its license yet, so it is not answering anyone. "
        "It tries again every 15 minutes. Send /license to try now."
    ),
}
# A button's popup is capped at 200 characters, so the admin gets a pointer.
LICENSE_LOCKED_ADMIN_BUTTON_MESSAGE = "Deskmate is locked. Send /license for details."

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


async def _register_commands(application: Application, admin_user_id: int) -> None:
    """Fill the "/" menu: /ask and /docs for everyone, plus the admin
    commands in the admin's own chat. A failure only affects the menu."""
    try:
        await application.bot.set_my_commands(STAFF_COMMANDS, scope=BotCommandScopeDefault())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not register the bot's commands: %s", exc)
    try:
        await application.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_user_id))
    except Exception as exc:  # noqa: BLE001
        # Normal until the admin has messaged the bot once.
        logger.info("Could not register the admin command menu yet: %s", exc)


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
        # Recent bot answers, (chat_id, message_id) -> the question asked, so a
        # reply to an answer can be sent with that exchange as context. And
        # the "What's your question?" prompts /ask sends, so a reply to one
        # is treated as a fresh question. Both are in memory and capped; after
        # a restart a reply to an older answer still gets the answer text
        # (Telegram includes it), just not the original question.
        self.answer_questions: OrderedDict[tuple[int, int], str] = OrderedDict()
        self.question_prompts: OrderedDict[tuple[int, int], None] = OrderedDict()
        self.license = licensing.LicenseManager(config.license_key)
        # The (status, reason) the admin was last alerted about, so a lock is
        # announced once, not on every check that finds it unchanged.
        self.license_alerted: tuple[str, str] | None = None


def _is_allowed_chat(state: BotState, chat_id: int) -> bool:
    if not state.config.allowed_chat_ids:
        return True
    return chat_id in state.config.allowed_chat_ids


def _is_admin(state: BotState, user_id: int) -> bool:
    return user_id == state.config.admin_user_id


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.bot_data["state"]
    await update.message.reply_text(
        GREETING.format(company_name=state.config.company_name, bot_username=context.bot.username)
    )


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
    report = doctor.run_doctor(state.config, state.corpus, license_status=state.license.status_line())
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


def _remember(store: OrderedDict, key: tuple[int, int], value) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > REMEMBERED_MESSAGES:
        store.popitem(last=False)


async def _answer_and_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: BotState,
    question: str,
    previous: PreviousExchange | None = None,
) -> None:
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
        response = await asyncio.wait_for(
            state.answer_engine.answer(question, previous=previous), timeout=ANSWER_TIMEOUT_SECONDS
        )
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

    sent = await context.bot.send_message(chat_id=chat_id, text=response)
    if library_too_large is None and response != API_UNREACHABLE_MESSAGE:
        message_id = getattr(sent, "message_id", None)
        if isinstance(message_id, int):
            _remember(state.answer_questions, (chat_id, message_id), question)
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
    await _answer_and_reply(buffer["update"], context, state, buffer["text"], previous=buffer.get("previous"))


GROUP_CHAT_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


TRIGGER_PRIVATE = "private"
TRIGGER_MENTION = "mention"
TRIGGER_REPLY = "reply"
TRIGGER_NONE = "none"


def extract_question(message: Message, bot_id: int, bot_username: str) -> str | None:
    """The question to answer, or None if this message isn't meant for the bot."""
    return classify_message(message, bot_id, bot_username)[0]


def classify_message(message: Message, bot_id: int, bot_username: str) -> tuple[str | None, str]:
    """(question, trigger): the question to answer (None if this message
    isn't meant for the bot) and what made it count (a TRIGGER_* value).

    In a private chat every message is a question. In a group, only a message
    that mentions the bot, or replies to one of the bot's messages, is; the
    rest is people talking to each other. This check runs whatever the bot's
    group privacy setting is: with privacy off Telegram delivers every group
    message, and answering them all would spam the group and bill Anthropic
    for each one."""
    text = message.text or ""
    if message.chat.type not in GROUP_CHAT_TYPES:
        return text, TRIGGER_PRIVATE

    replied_to = message.reply_to_message
    is_reply_to_bot = replied_to is not None and replied_to.from_user is not None and replied_to.from_user.id == bot_id

    mentioned = False
    for entity, entity_text in message.parse_entities([MessageEntity.MENTION, MessageEntity.TEXT_MENTION]).items():
        if entity.type == MessageEntity.MENTION and entity_text.lower() == f"@{bot_username}".lower():
            mentioned = True
        elif entity.type == MessageEntity.TEXT_MENTION and entity.user is not None and entity.user.id == bot_id:
            mentioned = True
            text = text.replace(entity_text, " ", 1)

    if not (mentioned or is_reply_to_bot):
        return None, TRIGGER_NONE
    text = re.sub(rf"@{re.escape(bot_username)}\b", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split()), TRIGGER_MENTION if mentioned else TRIGGER_REPLY


def previous_exchange(message: Message, state: BotState, bot_id: int) -> PreviousExchange | None:
    """Context for a follow-up: set only when the message replies to one of
    the bot's answers. A reply to a greeting, an error, or a "What's your
    question?" prompt is a fresh question."""
    replied_to = message.reply_to_message
    if replied_to is None or replied_to.from_user is None or replied_to.from_user.id != bot_id:
        return None
    key = (message.chat.id, replied_to.message_id)
    if key in state.question_prompts:
        return None
    answer_text = replied_to.text or ""
    if key in state.answer_questions:
        return PreviousExchange(question=state.answer_questions[key], answer=answer_text)
    if answer_text.startswith(ANSWER_FIRST_LINE_PREFIX):
        # An answer from before a restart: the question is gone, the answer isn't.
        return PreviousExchange(question=None, answer=answer_text)
    return None


async def handle_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ask <question>: works in any chat, and in a group with privacy on
    it's how a question reaches the bot without a mention."""
    state: BotState = context.bot_data["state"]
    if not _is_allowed_chat(state, update.effective_chat.id):
        return
    parts = (update.message.text or "").split(maxsplit=1)
    question = parts[1].strip() if len(parts) > 1 else ""
    if not question:
        # Picking /ask from the menu sends it straight away with no question.
        # Ask for it with a forced reply aimed at this person only; with
        # privacy on, Telegram delivers the reply to the bot.
        prompt = await update.message.reply_text(
            ASK_PROMPT_MESSAGE,
            reply_markup=ForceReply(selective=True, input_field_placeholder="Type your question"),
            do_quote=True,
        )
        message_id = getattr(prompt, "message_id", None)
        if isinstance(message_id, int):
            _remember(state.question_prompts, (update.effective_chat.id, message_id), None)
        return
    logger.info("Question received via /ask in a %s chat", update.effective_chat.type)
    await _answer_and_reply(update, context, state, question)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.bot_data["state"]

    if update.message is None or not update.message.text:
        return

    chat_id = update.effective_chat.id
    if not _is_allowed_chat(state, chat_id):
        return

    question, trigger = classify_message(update.message, context.bot.id, context.bot.username)
    if update.effective_chat.type in GROUP_CHAT_TYPES:
        # Which kind of group message arrived, never its content, so it's
        # possible to tell "never delivered" apart from "delivered, ignored".
        logger.info("Group message received, trigger: %s", trigger)
    if question is None:
        return
    if not question:
        await update.message.reply_text(GROUP_EMPTY_QUESTION_MESSAGE.format(bot_username=context.bot.username))
        return

    # A Telegram client sometimes splits one long paste into several
    # consecutive messages. Merge them within the debounce window instead
    # of answering each fragment separately.
    key = (chat_id, update.effective_user.id)
    buffer = state.pending_buffers.get(key)
    if buffer is not None:
        buffer["timer_task"].cancel()
        buffer["text"] += "\n" + question
        buffer["update"] = update
    else:
        buffer = {
            "text": question,
            "update": update,
            "previous": previous_exchange(update.message, state, context.bot.id),
        }
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


LICENSE_ALERT_KIND = "license"
# Commands some handler below answers, so a locked bot replies to them and
# stays silent on commands meant for other bots.
KNOWN_COMMANDS = frozenset({"start", "docs", "ask", "doctor", "reset_demo", "remove", "license"})


def _command_of(message: Message | None, bot_username: str | None) -> str | None:
    """The lower-case command a message starts with, or None if it isn't a
    command for this bot (/cmd@other_bot is someone else's)."""
    if message is None or not (message.text or "").startswith("/"):
        return None
    command, _, target = message.text.split(maxsplit=1)[0][1:].partition("@")
    if target and target.lower() != (bot_username or "").lower():
        return None
    return command.lower()


def _locked_reply_wanted(update: Update, state: BotState, bot) -> bool:
    """Whether the bot would have replied to this message if it weren't
    locked. A locked bot stays as quiet as an unlocked one: no reply to group
    chatter, chats outside ALLOWED_CHAT_IDS, or updates with no message."""
    message = update.message
    if message is None:
        return False
    if message.document is not None:
        return True
    command = _command_of(message, bot.username)
    if command is not None:
        if command not in KNOWN_COMMANDS:
            return False
        return command != "ask" or _is_allowed_chat(state, message.chat.id)
    if not message.text or not _is_allowed_chat(state, message.chat.id):
        return False
    question, _ = classify_message(message, bot.id, bot.username)
    return question is not None


def _license_admin_message(state: BotState) -> str:
    reason = state.license.lock_reason
    return LICENSE_LOCKED_ADMIN_MESSAGES.get(reason, LICENSE_LOCKED_ADMIN_BUTTON_MESSAGE)


async def license_gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every real handler. While the license is not active it
    stops every update, except the admin's /license. It never touches what
    the bot sends on its own (admin alerts), only updates coming in."""
    state: BotState = context.bot_data["state"]
    if state.license.is_active:
        return
    if isinstance(update, Update):
        user = update.effective_user
        is_admin = user is not None and _is_admin(state, user.id)
        if is_admin and _command_of(update.message, context.bot.username) == "license":
            return
        try:
            if update.callback_query is not None:
                # Answer it, or the button keeps spinning.
                await update.callback_query.answer(
                    LICENSE_LOCKED_ADMIN_BUTTON_MESSAGE if is_admin else LICENSE_LOCKED_STAFF_MESSAGE,
                    show_alert=True,
                )
            elif _locked_reply_wanted(update, state, context.bot):
                await update.message.reply_text(
                    _license_admin_message(state) if is_admin else LICENSE_LOCKED_STAFF_MESSAGE
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not send the locked reply: %s", exc)
    raise ApplicationHandlerStop


async def announce_license_status(bot, state: BotState) -> None:
    """Tell the admin once when the bot locks, or starts up locked. Nothing
    is sent while the status stays the same."""
    if state.license.is_active:
        state.license_alerted = None
        return
    current = (state.license.state.status, state.license.state.reason)
    if current == state.license_alerted:
        return
    state.license_alerted = current
    # The dedupe above decides; _alert_admin's hourly throttle must not
    # swallow a new lock that follows an unlock.
    state.admin_alert_sent_at.pop(LICENSE_ALERT_KIND, None)
    await _alert_admin(SimpleNamespace(bot=bot), state, LICENSE_ALERT_KIND, _license_admin_message(state))


def _format_check_time(value: str | None) -> str:
    if not value:
        return "never"
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


async def handle_license(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/license: check with Gumroad now and report. Admin only; works while
    the bot is locked."""
    state: BotState = context.bot_data["state"]
    if not _is_admin(state, update.effective_user.id):
        await update.message.reply_text(ADMIN_ONLY_MESSAGE)
        return
    summary = await state.license.check()
    # The admin is reading the result right now; no separate alert needed.
    state.license_alerted = (
        None if state.license.is_active else (state.license.state.status, state.license.state.reason)
    )
    license_state = state.license.state
    lines = [
        f"License: {state.license.status_line()}",
        f"Key: {licensing.mask_key(state.license.key)}",
        f"Last confirmed valid: {_format_check_time(license_state.last_ok_at)}",
        f"Last answer from Gumroad: {_format_check_time(license_state.last_check_at)}",
        f"Just now: {summary}",
    ]
    if not state.license.is_active:
        lines += ["", _license_admin_message(state)]
    await update.message.reply_text("\n".join(lines))


# The health marker runs first for every update, then the license gate. The
# gate stops blocked updates with ApplicationHandlerStop, which only skips
# later groups, so a blocked update is still marked processed and the
# queue-stall check (and so the exit watchdog) never sees a false stall.
MARK_PROCESSED_GROUP = -2
LICENSE_GATE_GROUP = -1


def register_handlers(application: Application, polling_health: health.PollingHealth) -> None:
    async def _mark_update_processed(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        polling_health.record_update_processed()

    application.add_handler(TypeHandler(object, _mark_update_processed), group=MARK_PROCESSED_GROUP)
    application.add_handler(TypeHandler(object, license_gate), group=LICENSE_GATE_GROUP)
    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(CommandHandler("docs", handle_docs))
    application.add_handler(CommandHandler("ask", handle_ask))
    application.add_handler(CommandHandler("doctor", handle_doctor))
    application.add_handler(CommandHandler("reset_demo", handle_reset_demo))
    application.add_handler(CommandHandler("remove", handle_remove))
    application.add_handler(CommandHandler("license", handle_license))
    application.add_handler(CallbackQueryHandler(handle_remove_button, pattern=rf"^{REMOVE_CALLBACK_PREFIX}:"))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(handle_error)


def main() -> None:
    config = load_config()
    logging.getLogger().setLevel(config.log_level)

    redact_filter = RedactSecretsFilter(
        [config.telegram_bot_token, config.anthropic_api_key, config.healthcheck_ping_url or "", config.license_key]
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
    license_client: httpx.AsyncClient | None = None

    async def _start_monitoring(application: Application) -> None:
        nonlocal heartbeat_client, license_client
        await _register_commands(application, config.admin_user_id)
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

        # Its own client: the heartbeat's only exists when HEALTHCHECK_PING_URL is set.
        license_client = httpx.AsyncClient(timeout=licensing.REQUEST_TIMEOUT_SECONDS)
        state.license.client = license_client
        try:
            await state.license.check()
            await announce_license_status(application.bot, state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Startup license check failed: %s: %s", type(exc).__name__, exc)
        background_tasks.append(
            asyncio.create_task(
                licensing.check_loop(state.license, lambda: announce_license_status(application.bot, state))
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
        if license_client is not None:
            await license_client.aclose()

    builder = Application.builder().bot(bot).post_init(_start_monitoring).post_shutdown(_stop_monitoring)
    application = builder.build()
    application.bot_data["state"] = state
    register_handlers(application, polling_health)

    logger.info("Deskmate starting for %s (%d documents loaded)", config.company_name, len(corpus))
    health.start_exit_watchdog(polling_health)
    application.run_polling(allowed_updates=Update.ALL_TYPES, timeout=health.POLL_TIMEOUT_SECONDS)


if __name__ == "__main__":
    main()
