"""Library size and cost report, sent to the admin after each upload.

Every question sends the full text of every document to Anthropic, so the
library's size sets both the cost per question and how close the bot is to
the model's context window (past it, every question fails). This module only
measures and reports; it never blocks an upload.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

logger = logging.getLogger("deskmate.library_size")

# First-party API prices in US$ per million tokens (input, output), checked
# 2026-09-27. A model missing here gets a size report without a cost line.
PRICES_PER_MTOK = {
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
# The document block is cached with the default 5-minute TTL: the first
# question after a quiet spell writes the cache, later ones read it.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10
# Answers averaged about 70 output tokens on the demo documents; rounded up.
TYPICAL_OUTPUT_TOKENS = 100

COST_WARNING_USD = 0.50
CONTEXT_WARNING_FRACTION = 0.80

# Used only when the Models API can't be reached. The smallest context window
# among current models, so the report errs towards warning early.
FALLBACK_CONTEXT_TOKENS = 200_000
# Used only when count_tokens can't be reached. The demo documents measured
# about 3.9 characters per token; a lower figure overestimates on purpose.
OFFLINE_CHARS_PER_TOKEN = 3

_context_window_cache: dict[str, int] = {}


@dataclass(frozen=True)
class LibrarySize:
    tokens: int
    tokens_exact: bool
    context_tokens: int
    context_exact: bool

    @property
    def context_fraction(self) -> float:
        return self.tokens / self.context_tokens


def estimate_tokens_offline(static_block: str) -> int:
    return math.ceil(len(static_block) / OFFLINE_CHARS_PER_TOKEN)


def measure(client, model: str, static_block: str) -> LibrarySize:
    """Token count of the prompt every question would send, plus the model's
    context window. Blocking (sync SDK client): run it in an executor. Falls
    back to conservative estimates rather than raising."""
    try:
        count = client.messages.count_tokens(
            model=model,
            system=[{"type": "text", "text": static_block}],
            messages=[{"role": "user", "content": "How long is probation?"}],
        )
        tokens, tokens_exact = count.input_tokens, True
    except Exception as exc:  # noqa: BLE001
        logger.warning("count_tokens failed, using an offline estimate: %s", exc)
        tokens, tokens_exact = estimate_tokens_offline(static_block), False

    context_tokens = _context_window_cache.get(model)
    context_exact = context_tokens is not None
    if context_tokens is None:
        try:
            context_tokens = client.models.retrieve(model).max_input_tokens
            _context_window_cache[model] = context_tokens
            context_exact = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not look up the context window for %s: %s", model, exc)
            context_tokens = FALLBACK_CONTEXT_TOKENS

    return LibrarySize(tokens, tokens_exact, context_tokens, context_exact)


def question_costs(model: str, tokens: int) -> tuple[float, float] | None:
    """(cold, warm) US$ per question: cold writes the document cache, warm
    reads it (a question within 5 minutes of the previous one)."""
    prices = PRICES_PER_MTOK.get(model)
    if prices is None:
        return None
    input_price, output_price = prices
    output_cost = TYPICAL_OUTPUT_TOKENS * output_price / 1_000_000
    cold = tokens * input_price * CACHE_WRITE_MULTIPLIER / 1_000_000 + output_cost
    warm = tokens * input_price * CACHE_READ_MULTIPLIER / 1_000_000 + output_cost
    return cold, warm


def format_usd(amount: float) -> str:
    if amount < 0.001:
        return "less than US$0.001"
    if amount < 0.10:
        return f"about US${amount:.3f}"
    return f"about US${amount:.2f}"


def format_report(size: LibrarySize, model: str, document_count: int) -> str:
    plural = "s" if document_count != 1 else ""
    estimate_note = " (rough estimate)" if not size.tokens_exact else ""
    percent = size.context_fraction * 100
    lines = [
        f"Your library now has {document_count} document{plural}, about {size.tokens:,} tokens{estimate_note}. "
        f"That's {percent:.0f}% of what the AI model can read at once."
    ]

    costs = question_costs(model, size.tokens)
    if costs is not None:
        cold, warm = costs
        lines.append(
            f"Estimated cost per question: {format_usd(cold)}, or {format_usd(warm)} "
            "if it comes within 5 minutes of the previous question."
        )

    if size.context_fraction >= CONTEXT_WARNING_FRACTION:
        lines.append(
            "Warning: your documents are close to the limit. At 100% the bot stops answering every "
            "question. Remove documents you no longer need with /remove before adding more."
        )
    elif costs is not None and costs[0] > COST_WARNING_USD:
        lines.append(
            "Heads up: that's a lot per question. Removing documents you no longer need with "
            "/remove brings it down."
        )
    return "\n\n".join(lines)
