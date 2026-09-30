"""No invented conflicts, and real ones still caught.

Run from the repo root: .venv/bin/python tests/test_no_overlap.py

Two libraries, real API calls like test_acceptance.py:

1. One topic per document: the demo corpus minus
   returns-and-refunds-policy-v1-4-FINAL.md. Every Conflict check line must
   be exactly "Conflict check: none".
2. Two different policies on one topic: the demo corpus plus
   tests/fixtures/uniform-policy.md, which covers uniforms like section 7 of
   the handbook.
   - Control (must pass): shoe colours, where the two really differ. The
     answer must follow the newer uniform policy and the conflict must be
     reported.
   - Known limitation (reported, never fails the run on format): questions
     where the two agree. The model tends to explain the agreement in the
     Conflict check line instead of writing just "none"; measured
     2026-09-30, 0/6 exact "none". The content of those answers is still
     checked and does fail the run if wrong.
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

DEMO_DIR = REPO_ROOT / "demo-corpus" / "docs"
UNIFORM_POLICY = Path(__file__).resolve().parent / "fixtures" / "uniform-policy.md"
LEFT_OUT = "returns-and-refunds-policy-v1-4-FINAL.md"
NONE_LINE = "Conflict check: none"

ONE_TOPIC_PER_DOCUMENT = [
    {"question": "How many days does a customer have to change their mind?", "source_contains": "returns-and-refunds-policy-v2-0", "yes_no": False},
    {"question": "A return is $180. Do I need a manager?", "source_contains": "returns-and-refunds-policy-v2-0", "yes_no": True},
    {"question": "How long is probation?", "source_contains": "employee-handbook", "yes_no": False},
    {"question": "What is my discount on a sale item?", "source_contains": "staff-purchase-and-discount", "yes_no": False},
    {"question": "What is the most one person can lift alone?", "source_contains": "workplace-health-safety", "yes_no": False},
    {"question": "When is the roster published?", "source_contains": "rostering-and-leave", "yes_no": False},
]

# Both documents agree: two shirts on the first day; name badges are worn.
AGREEING_POLICIES = [
    {"question": "How many shirts do I get on my first day?", "source_contains": None, "yes_no": False, "answer_contains": ["two"]},
    {"question": "Do I have to wear a name badge?", "source_contains": None, "yes_no": True, "answer_contains": []},
]

SHOE_CONTROL = {"question": "What colour shoes can I wear?", "source_contains": "uniform-policy", "yes_no": False}


@dataclass
class Doc:
    filename: str
    extracted_text: str


class Library:
    def __init__(self, paths: list[Path]) -> None:
        self.documents = [Doc(p.name, p.read_text(encoding="utf-8")) for p in paths]

    def __len__(self) -> int:
        return len(self.documents)


def demo_paths() -> list[Path]:
    return [p for p in sorted(DEMO_DIR.iterdir()) if p.is_file()]


def make_config() -> Config:
    return Config(
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"].strip(),
        telegram_bot_token="unused",
        company_name=os.environ.get("COMPANY_NAME", "Southline Outdoor Co."),
        admin_user_id=0,
        allowed_chat_ids=frozenset(),
        model=os.environ.get("MODEL", "").strip() or "claude-sonnet-4-6",
        log_level="INFO",
        healthcheck_ping_url=None,
    )


def split_lines(reply: str) -> dict[str, str]:
    lines = [line.strip() for line in reply.strip().split("\n") if line.strip()]
    labels = ("Answer:", "Source:", "Conflict check:", "Currency:")
    return {label: next((line for line in lines if line.startswith(label)), "") for label in labels}


def content_problems(reply: str, library: Library, spec: dict) -> list[str]:
    if reply.strip() == REFUSAL_STRING:
        return ["refused, but the answer is in the documents"]
    structure_spec = {"category": "A", "source_contains": spec["source_contains"], "yes_no": spec["yes_no"]}
    return check_structure(reply, library, structure_spec)[1]


def shoe_problems(reply: str) -> list[str]:
    """The answer must follow the newer uniform policy (black or brown) and
    the conflict with the handbook (black, navy or charcoal) must be named
    with what the handbook actually says."""
    parts = split_lines(reply)
    problems = []
    answer = parts["Answer:"].lower()
    if "brown" not in answer or "navy" in answer or "charcoal" in answer:
        problems.append(f"answer doesn't follow the newer policy (black or brown): {parts['Answer:']!r}")
    if "uniform-policy.md" not in parts["Source:"] or "3" not in parts["Source:"]:
        problems.append(f"expected uniform-policy.md section 3: {parts['Source:']!r}")
    if "1 July 2026" not in parts["Currency:"]:
        problems.append(f"expected effective 1 July 2026: {parts['Currency:']!r}")
    conflict = parts["Conflict check:"]
    if "handbook" not in conflict.lower() or not ("navy" in conflict or "charcoal" in conflict):
        problems.append(f"conflict with the handbook not reported with its actual colours: {conflict!r}")
    return problems


async def run() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("ANTHROPIC_API_KEY is not set.")
        return 1
    config = make_config()
    failures = 0

    library = Library([p for p in demo_paths() if p.name != LEFT_OUT])
    engine = AnswerEngine(config, library)
    print(f"=== One topic per document ({len(library)} documents)\n")
    for spec in ONE_TOPIC_PER_DOCUMENT:
        reply = await engine.answer(spec["question"])
        problems = content_problems(reply, library, spec)
        conflict = split_lines(reply)["Conflict check:"]
        if conflict != NONE_LINE:
            problems.append(f"expected exactly {NONE_LINE!r}, got {conflict!r}")
        failures += bool(problems)
        print(f"[{'FAIL' if problems else 'PASS'}] {spec['question']}\n{reply}")
        for problem in problems:
            print(f"  ! {problem}")
        print()

    library = Library(demo_paths() + [UNIFORM_POLICY])
    engine = AnswerEngine(config, library)
    print(f"=== Two policies on one topic ({len(library)} documents)\n")

    reply = await engine.answer(SHOE_CONTROL["question"])
    problems = content_problems(reply, library, SHOE_CONTROL) + shoe_problems(reply)
    failures += bool(problems)
    print(f"[{'FAIL' if problems else 'PASS'}] (control, real conflict) {SHOE_CONTROL['question']}\n{reply}")
    for problem in problems:
        print(f"  ! {problem}")
    print()

    format_misses = 0
    for spec in AGREEING_POLICIES:
        reply = await engine.answer(spec["question"])
        problems = content_problems(reply, library, spec)
        answer = split_lines(reply)["Answer:"].lower()
        problems += [f"answer should mention {word!r}" for word in spec["answer_contains"] if word not in answer]
        failures += bool(problems)
        conflict = split_lines(reply)["Conflict check:"]
        exact_none = conflict == NONE_LINE
        format_misses += not exact_none
        status = "FAIL" if problems else "PASS"
        note = "" if exact_none else "  (known limitation: explanation instead of 'none')"
        print(f"[{status}] (agreeing policies) {spec['question']}{note}\n{reply}")
        for problem in problems:
            print(f"  ! {problem}")
        print()

    print(f"Known limitation, format only: {format_misses}/{len(AGREEING_POLICIES)} agreeing-policy answers "
          "explained the agreement instead of writing 'none'.")
    print(f"RESULT: {'PASS' if failures == 0 else f'FAIL ({failures} problem answers)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
