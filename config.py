"""Load and validate configuration from environment variables."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

logger = logging.getLogger("deskmate.config")

REQUIRED_VARS = (
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "COMPANY_NAME",
    "ADMIN_USER_ID",
)

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_LOG_LEVEL = "INFO"


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    telegram_bot_token: str
    company_name: str
    admin_user_id: int
    allowed_chat_ids: frozenset[int]
    model: str
    log_level: str


def _fail(message: str) -> None:
    logger.error(message)
    sys.exit(1)


def load_config() -> Config:
    """Validate required env vars are present and non-empty, then build Config.

    Exits the process with status 1 and a single log line if any required
    variable is missing or empty. Deskmate must never run half-configured.
    """
    for var in REQUIRED_VARS:
        value = os.environ.get(var, "").strip()
        if not value:
            _fail(f"Missing required environment variable: {var}")

    admin_user_id_raw = os.environ["ADMIN_USER_ID"].strip()
    try:
        admin_user_id = int(admin_user_id_raw)
    except ValueError:
        _fail(f"ADMIN_USER_ID must be a numeric Telegram user id, got: {admin_user_id_raw!r}")
        raise  # unreachable, keeps type-checkers happy

    allowed_chat_ids_raw = os.environ.get("ALLOWED_CHAT_IDS", "").strip()
    allowed_chat_ids = frozenset(
        int(chat_id.strip())
        for chat_id in allowed_chat_ids_raw.split(",")
        if chat_id.strip()
    )

    return Config(
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"].strip(),
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"].strip(),
        company_name=os.environ["COMPANY_NAME"].strip(),
        admin_user_id=admin_user_id,
        allowed_chat_ids=allowed_chat_ids,
        model=os.environ.get("MODEL", "").strip() or DEFAULT_MODEL,
        log_level=os.environ.get("LOG_LEVEL", "").strip() or DEFAULT_LOG_LEVEL,
    )
