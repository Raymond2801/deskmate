"""Acceptance test runner: 20 fixed questions against the demo corpus.

Run from the repo root: .venv/bin/python tests/test_acceptance.py

This is the gate described in the build spec: Telegram wiring does not start
until this hits its thresholds. The 20 questions and their expected behaviour
are fixed in tests/test_questions.md — do not invent new ones here.

Automated checks only catch structural failures (line count, labels, whether
a cited source file actually exists, whether a yes/no answer starts with the
right word). Whether the *content* of an answer is correct still needs a
human to read the printed transcript.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from answer import AnswerEngine  # noqa: E402
from config import Config  # noqa: E402
from corpus import Corpus  # noqa: E402

REFUSAL_STRING = (
    "I couldn't find this in the company documents I have. Please ask your "
    "manager."
)

# category -> (count, minimum passes required)
THRESHOLDS = {
    "A": (12, 11),
    "B": (3, 2),
    "C": (2, 1),
    "D": (3, 3),
}

# yes_no: question expects the Answer line to open with "Yes" or "No"
QUESTIONS = [
    {"id": 1, "category": "A", "question": "How long is my meal break on an 8 hour shift?", "yes_no": False, "source_contains": "employee-handbook"},
    {"id": 2, "category": "A", "question": "How long is probation?", "yes_no": False, "source_contains": "employee-handbook"},
    {"id": 3, "category": "A", "question": "When do I need a medical certificate?", "yes_no": False, "source_contains": "employee-handbook"},
    {"id": 4, "category": "A", "question": "How much notice do I need for annual leave?", "yes_no": False, "source_contains": "rostering-and-leave"},
    {"id": 5, "category": "A", "question": "Can I take annual leave in December?", "yes_no": True, "source_contains": "rostering-and-leave"},
    {"id": 6, "category": "A", "question": "When is the roster published?", "yes_no": False, "source_contains": "rostering-and-leave"},
    {"id": 7, "category": "A", "question": "I am sick and cannot come in. What do I do?", "yes_no": False, "source_contains": "rostering-and-leave"},
    {"id": 8, "category": "A", "question": "Can I swap a shift directly with a colleague?", "yes_no": True, "source_contains": "rostering-and-leave"},
    {"id": 9, "category": "A", "question": "What is my discount on a sale item?", "yes_no": False, "source_contains": "staff-purchase-and-discount"},
    {"id": 10, "category": "A", "question": "Can I use my discount to buy a jacket for my brother?", "yes_no": True, "source_contains": "staff-purchase-and-discount"},
    {"id": 11, "category": "A", "question": "What is the most one person can lift alone?", "yes_no": False, "source_contains": "workplace-health-safety"},
    {"id": 12, "category": "A", "question": "I am new and unsure about something. Who do I ask?", "yes_no": False, "source_contains": "new-starter-onboarding"},
    {"id": 13, "category": "B", "question": "How many days does a customer have to change their mind?", "yes_no": False, "source_contains": "returns-and-refunds-policy-v2-0"},
    {"id": 14, "category": "B", "question": "Change of mind: can the customer get a refund, or only store credit?", "yes_no": False, "source_contains": "returns-and-refunds-policy-v2-0"},
    {"id": 15, "category": "B", "question": "A return is $180. Do I need a manager?", "yes_no": True, "source_contains": "returns-and-refunds-policy-v2-0"},
    {"id": 16, "category": "C", "question": "What is the till float at opening?", "yes_no": False, "source_contains": "store-opening-closing"},
    {"id": 17, "category": "C", "question": "Do two people need to close after 6pm?", "yes_no": True, "source_contains": "store-opening-closing"},
    {"id": 18, "category": "D", "question": "What is the parental leave policy?", "yes_no": False, "source_contains": None},
    {"id": 19, "category": "D", "question": "Which super fund does the company use?", "yes_no": False, "source_contains": None},
    {"id": 20, "category": "D", "question": "Am I allowed to work a second job at another retailer?", "yes_no": False, "source_contains": None},
]

LABELS = ("Answer:", "Source:", "Conflict check:", "Currency:")


def check_structure(response: str, corpus: Corpus, spec: dict) -> tuple[bool, list[str]]:
    problems: list[str] = []

    if spec["category"] == "D":
        if response.strip() != REFUSAL_STRING:
            problems.append("did not return the exact refusal string")
        return (not problems, problems)

    lines = [line for line in response.strip().split("\n") if line.strip()]
    if len(lines) != 4:
        problems.append(f"expected 4 lines, got {len(lines)}")
        return (False, problems)

    for line, label in zip(lines, LABELS):
        if not line.startswith(label):
            problems.append(f"expected line to start with {label!r}, got {line!r}")

    if problems:
        return (False, problems)

    source_line = lines[1]
    known_filenames = {doc.filename for doc in corpus.documents}
    if not any(name in source_line for name in known_filenames):
        problems.append(f"Source line cites no known corpus file: {source_line!r}")

    if spec["source_contains"] and spec["source_contains"] not in source_line:
        problems.append(f"expected source to mention {spec['source_contains']!r}: {source_line!r}")

    if spec["yes_no"]:
        answer_line = lines[0]
        first_word = answer_line[len("Answer:"):].strip().split()[0].rstrip(".,") if len(answer_line) > len("Answer:") else ""
        if first_word not in ("Yes", "No"):
            problems.append(f"yes/no question but Answer line starts with {first_word!r}")

    return (not problems, problems)


async def run() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ANTHROPIC_API_KEY is not set. Cannot run acceptance tests.")
        return 1

    config = Config(
        anthropic_api_key=api_key,
        telegram_bot_token="unused-in-cli-tests",
        company_name=os.environ.get("COMPANY_NAME", "Southline Outdoor Co."),
        admin_user_id=0,
        allowed_chat_ids=frozenset(),
        model=os.environ.get("MODEL", "").strip() or "claude-sonnet-4-6",
        log_level="INFO",
        healthcheck_ping_url=None,
    )

    corpus = Corpus.load()
    print(f"Loaded corpus: {len(corpus)} files, {corpus.total_word_count()} words\n")

    engine = AnswerEngine(config, corpus)

    results_by_category: dict[str, list[bool]] = {cat: [] for cat in THRESHOLDS}

    for spec in QUESTIONS:
        response = await engine.answer(spec["question"])
        passed, problems = check_structure(response, corpus, spec)
        results_by_category[spec["category"]].append(passed)

        status = "PASS" if passed else "FAIL"
        print(f"[{status}] Q{spec['id']} ({spec['category']}): {spec['question']}")
        print(response)
        if problems:
            for problem in problems:
                print(f"  ! {problem}")
        print()

    print("=" * 60)
    print("Scoring sheet")
    print("=" * 60)
    overall_ok = True
    for category, (count, required) in THRESHOLDS.items():
        passed = sum(results_by_category[category])
        ok = passed >= required
        overall_ok = overall_ok and ok
        marker = "OK" if ok else "FAIL"
        print(f"Category {category}: {passed}/{count} passed (need {required}+)  [{marker}]")

    print()
    print("RESULT:", "PASS — gate cleared, Telegram work may proceed" if overall_ok else "FAIL — do not proceed to Telegram wiring")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
