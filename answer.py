"""Assemble the prompt and call the Anthropic API."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import anthropic

import doctor
from config import Config
from corpus import Corpus

logger = logging.getLogger("deskmate.answer")

SYSTEM_PROMPT_PATH = Path("prompts") / "system_prompt.md"
MAX_TOKENS = 1024
API_TIMEOUT_SECONDS = 60

API_UNREACHABLE_MESSAGE = "I could not reach the language model. Please try again in a moment."


def _load_system_prompt_template() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def build_static_block(config: Config, corpus: Corpus) -> str:
    """The cacheable half of the prompt: system rules plus every document,
    each wrapped in a "=== FILE: <filename> ===" header. answer() relies on
    this exact header format to cite sources, so it must not change shape."""
    system_prompt = _load_system_prompt_template().replace("{{COMPANY_NAME}}", config.company_name)

    file_blocks = [
        f"=== FILE: {doc.filename} ===\n{doc.extracted_text}" for doc in corpus.documents
    ]

    return system_prompt + "\n\n## Documents\n\n" + "\n\n".join(file_blocks)


class AnswerEngine:
    def __init__(self, config: Config, corpus: Corpus) -> None:
        self._config = config
        self._corpus = corpus
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    async def answer(self, question: str) -> str:
        """Answer a single question independently. No conversation history
        is sent: this is a lookup tool, not a chat, and history would let a
        stale answer bias a later question."""
        static_block = build_static_block(self._config, self._corpus)

        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._call_api, static_block, question),
                timeout=API_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - any API failure must fall back safely
            logger.error("Anthropic API call failed: %s", exc)
            doctor.record_last_error(f"Anthropic API call failed: {exc}")
            return API_UNREACHABLE_MESSAGE

    def _call_api(self, static_block: str, question: str) -> str:
        message = self._client.messages.create(
            model=self._config.model,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": static_block,
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[
                {"role": "user", "content": question},
            ],
        )
        return "".join(block.text for block in message.content if block.type == "text")
