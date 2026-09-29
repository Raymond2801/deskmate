"""No invented conflicts: a library where no two documents share a topic.

Run from the repo root: .venv/bin/python tests/test_no_overlap.py

The demo corpus minus returns-and-refunds-policy-v1-4-FINAL.md, so every
topic is covered by exactly one document. Every answer's Conflict check line
must be "none"; anything else is a conflict the bot made up. Real API calls,
like test_acceptance.py.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from answer import AnswerEngine  # noqa: E402
from config import Config  # noqa: E402
from test_acceptance import REFUSAL_STRING, check_structure  # noqa: E402

LEFT_OUT = "returns-and-refunds-policy-v1-4-FINAL.md"

QUESTIONS = [
    {"question": "How many days does a customer have to change their mind?", "source_contains": "returns-and-refunds-policy-v2-0", "yes_no": False},
    {"question": "A return is $180. Do I need a manager?", "source_contains": "returns-and-refunds-policy-v2-0", "yes_no": True},
    {"question": "How long is probation?", "source_contains": "employee-handbook", "yes_no": False},
    {"question": "What is my discount on a sale item?", "source_contains": "staff-purchase-and-discount", "yes_no": False},
    {"question": "What is the most one person can lift alone?", "source_contains": "workplace-health-safety", "yes_no": False},
    {"question": "When is the roster published?", "source_contains": "rostering-and-leave", "yes_no": False},
]


@dataclass
class Doc:
    filename: str
    extracted_text: str


class OneTopicPerDocumentCorpus:
    def __init__(self) -> None:
        docs_dir = REPO_ROOT / "demo-corpus" / "docs"
        self.documents = [
            Doc(p.name, p.read_text(encoding="utf-8"))
            for p in sorted(docs_dir.iterdir())
            if p.is_file() and p.name != LEFT_OUT
        ]

    def __len__(self) -> int:
        return len(self.documents)


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
    corpus = OneTopicPerDocumentCorpus()
    print(f"Corpus: {len(corpus)} documents, without {LEFT_OUT}\n")
    engine = AnswerEngine(config, corpus)

    failures = 0
    for spec in QUESTIONS:
        reply = await engine.answer(spec["question"])
        problems: list[str] = []
        if reply.strip() == REFUSAL_STRING:
            problems.append("refused, but the answer is in the documents")
        else:
            _, problems = check_structure(reply, corpus, {"category": "A", **spec})
            lines = [line for line in reply.strip().split("\n") if line.strip()]
            conflict = next((line for line in lines if line.startswith("Conflict check:")), "")
            if conflict.strip() != "Conflict check: none":
                problems.append(f"invented a conflict: {conflict!r}")
        failures += bool(problems)
        print(f"[{'FAIL' if problems else 'PASS'}] {spec['question']}\n{reply}")
        for problem in problems:
            print(f"  ! {problem}")
        print()

    print(f"RESULT: {len(QUESTIONS) - failures}/{len(QUESTIONS)} answered with Conflict check: none")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
