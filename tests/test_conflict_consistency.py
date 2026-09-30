"""Repeat two conflict questions and check every answer's content.

Run from the repo root: .venv/bin/python tests/test_conflict_consistency.py
Repeat counts: SHOE_RUNS (default 5) and REFUND_RUNS (default 3).

Answers vary run to run, so one pass proves little. This asks each question
several times and checks every answer:

- Shoe colours (demo corpus + tests/fixtures/uniform-policy.md): two
  different policies disagree. Answer must follow the newer uniform policy
  (black or brown), cite uniform-policy.md section 3, give effective 1 July
  2026, and the Conflict check line must name the handbook with its actual
  colours. Its wording is free-form, a known format limitation that is
  reported, not failed.
- Change-of-mind days (demo corpus): two versions of one policy. Answer
  must be 30 days from v2.0 section 2, effective 1 September 2025, with the
  v1.4 policy (15 June 2023, 14 days) in the Conflict check line.

Measured 2026-09-30 with 10 + 5 runs: every answer correct on content.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from answer import AnswerEngine  # noqa: E402
from test_no_overlap import (  # noqa: E402
    UNIFORM_POLICY,
    Library,
    demo_paths,
    make_config,
    shoe_problems,
    split_lines,
)

SHOE_QUESTION = "What colour shoes can I wear?"
REFUND_QUESTION = "How many days does a customer have to change their mind?"
TEMPLATE_START = "Conflict check: an earlier version ("


def refund_problems(reply: str) -> list[str]:
    parts = split_lines(reply)
    problems = []
    if "30" not in parts["Answer:"]:
        problems.append(f"expected 30 days: {parts['Answer:']!r}")
    if "returns-and-refunds-policy-v2-0.md" not in parts["Source:"] or "2" not in parts["Source:"].split(".md")[-1]:
        problems.append(f"expected v2-0 section 2: {parts['Source:']!r}")
    if "1 September 2025" not in parts["Currency:"]:
        problems.append(f"expected effective 1 September 2025: {parts['Currency:']!r}")
    conflict = parts["Conflict check:"]
    for needed in ("v1-4", "14 days", "15 June 2023"):
        if needed not in conflict:
            problems.append(f"Conflict check line misses {needed!r}: {conflict!r}")
    return problems


async def repeat(engine: AnswerEngine, question: str, runs: int, check) -> tuple[int, int]:
    failures = on_template = 0
    for i in range(1, runs + 1):
        reply = await engine.answer(question)
        problems = check(reply)
        failures += bool(problems)
        conflict = split_lines(reply)["Conflict check:"]
        on_template += conflict.startswith(TEMPLATE_START)
        print(f"[{'FAIL' if problems else 'PASS'}] run {i}: {question}\n{reply}")
        for problem in problems:
            print(f"  ! {problem}")
        print()
    return failures, on_template


async def run() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("ANTHROPIC_API_KEY is not set.")
        return 1
    shoe_runs = int(os.environ.get("SHOE_RUNS", "5"))
    refund_runs = int(os.environ.get("REFUND_RUNS", "3"))
    config = make_config()

    shoe_failures, shoe_template = await repeat(
        AnswerEngine(config, Library(demo_paths() + [UNIFORM_POLICY])), SHOE_QUESTION, shoe_runs, shoe_problems
    )
    refund_failures, refund_template = await repeat(
        AnswerEngine(config, Library(demo_paths())), REFUND_QUESTION, refund_runs, refund_problems
    )

    print(f"Shoe colours:       {shoe_runs - shoe_failures}/{shoe_runs} correct on content; "
          f"{shoe_template}/{shoe_runs} Conflict check lines on the template (free-form is a known limitation)")
    print(f"Change-of-mind days: {refund_runs - refund_failures}/{refund_runs} correct on content; "
          f"{refund_template}/{refund_runs} Conflict check lines on the template")
    failed = shoe_failures + refund_failures
    print(f"RESULT: {'PASS' if failed == 0 else f'FAIL ({failed} wrong answers)'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
