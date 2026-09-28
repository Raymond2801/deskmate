"""Follow-up questions against the demo corpus (real API calls).

Run from the repo root: .venv/bin/python tests/test_followups.py

A follow-up is a reply to one of the bot's answers; the bot sends that
exchange as context. Each case runs twice: with the original question
remembered, and with only the answer text (what the bot has after a
restart). Like test_acceptance.py, the automated check is structural (four
labelled lines citing a real file, or the exact refusal); read the printed
transcript to judge whether the content is right.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from answer import AnswerEngine, PreviousExchange  # noqa: E402
from config import Config  # noqa: E402
from corpus import Corpus  # noqa: E402
from test_acceptance import REFUSAL_STRING, check_structure  # noqa: E402

CASES = [
    {"first": "How long is probation?", "follow_up": "And for casuals?", "source_contains": None, "yes_no": False},
    {
        "first": "What is my discount on a sale item?",
        "follow_up": "Can I use it to buy a jacket for my brother?",
        "source_contains": "staff-purchase-and-discount",
        "yes_no": True,
    },
    {
        "first": "How much notice do I need for annual leave?",
        "follow_up": "Is it different in December?",
        "source_contains": "rostering-and-leave",
        "yes_no": False,
    },
]


async def run() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ANTHROPIC_API_KEY is not set.")
        return 1
    config = Config(
        anthropic_api_key=api_key,
        telegram_bot_token="unused",
        company_name=os.environ.get("COMPANY_NAME", "Southline Outdoor Co."),
        admin_user_id=0,
        allowed_chat_ids=frozenset(),
        model=os.environ.get("MODEL", "").strip() or "claude-sonnet-4-6",
        log_level="INFO",
        healthcheck_ping_url=None,
    )
    corpus = Corpus.load()
    engine = AnswerEngine(config, corpus)

    failures = 0
    for i, case in enumerate(CASES, 1):
        first_answer = await engine.answer(case["first"])
        print(f"=== Case {i}\nQ: {case['first']}\n{first_answer}\n")
        for mode, previous in (
            ("with question", PreviousExchange(question=case["first"], answer=first_answer)),
            ("answer only", PreviousExchange(question=None, answer=first_answer)),
        ):
            reply = await engine.answer(case["follow_up"], previous=previous)
            if reply.strip() == REFUSAL_STRING:
                ok, problems = True, []
            else:
                spec = {"category": "A", "source_contains": case["source_contains"], "yes_no": case["yes_no"]}
                ok, problems = check_structure(reply, corpus, spec)
            failures += not ok
            print(f"--- Follow-up ({mode}): {case['follow_up']}  [{'OK' if ok else 'FAIL'}]")
            print(reply)
            for problem in problems:
                print(f"  ! {problem}")
            print()

    total = len(CASES) * 2
    print(f"RESULT: {total - failures}/{total} follow-ups structurally OK")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
