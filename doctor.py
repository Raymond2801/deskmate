"""Local self-diagnostics. No network calls, no API calls."""

from __future__ import annotations

import platform

from corpus import DATA_DIR, Corpus
from config import Config

LAST_ERROR_FILE = DATA_DIR / "last_error.txt"


def record_last_error(message: str) -> None:
    """Overwrite the last-error file so /doctor can surface it."""
    from datetime import datetime, timezone

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    LAST_ERROR_FILE.write_text(f"{timestamp} {message}\n", encoding="utf-8")


def _read_last_error() -> str:
    if LAST_ERROR_FILE.exists():
        content = LAST_ERROR_FILE.read_text(encoding="utf-8").strip()
        return content or "none"
    return "none"


def _mask_secret(value: str) -> str:
    if not value:
        return "missing"
    tail = value[-4:] if len(value) >= 4 else value
    return f"set (...{tail})"


def _data_dir_writable() -> bool:
    probe = DATA_DIR / ".doctor_write_probe"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def run_doctor(config: Config, corpus: Corpus) -> str:
    lines = ["Deskmate doctor"]
    lines.append(f"Python: {platform.python_version()}")
    lines.append(f"ANTHROPIC_API_KEY: {_mask_secret(config.anthropic_api_key)}")
    lines.append(f"TELEGRAM_BOT_TOKEN: {_mask_secret(config.telegram_bot_token)}")
    lines.append(f"COMPANY_NAME: {config.company_name}")
    lines.append(f"ADMIN_USER_ID: {config.admin_user_id}")

    total_words = corpus.total_word_count()
    lines.append(f"Documents: {len(corpus)} files, {total_words:,} words total")
    for doc in corpus.documents:
        lines.append(f"  {doc.filename:<30} {doc.word_count:,} words")

    lines.append(f"Data directory: {'writable' if _data_dir_writable() else 'NOT WRITABLE'}")
    lines.append(f"Last error: {_read_last_error()}")

    return "\n".join(lines)
