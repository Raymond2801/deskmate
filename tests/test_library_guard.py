"""Unit tests for the document-library size guard.

Run from the repo root: .venv/bin/python -m unittest tests.test_library_guard -v
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402

import answer  # noqa: E402
import bot  # noqa: E402
from config import Config  # noqa: E402

ADMIN_ID = 111
STAFF_CHAT_ID = -222
TOO_LONG = "prompt is too long: 1060682 tokens > 1000000 maximum"


def make_config() -> Config:
    return Config(
        anthropic_api_key="sk-test",
        telegram_bot_token="123:TEST",
        company_name="Test Co.",
        admin_user_id=ADMIN_ID,
        allowed_chat_ids=frozenset(),
        model="claude-sonnet-4-6",
        log_level="INFO",
        healthcheck_ping_url=None,
    )


def bad_request(message: str) -> anthropic.BadRequestError:
    # A stand-in response keeps this independent of which HTTP library the
    # installed SDK version uses.
    response = mock.Mock(status_code=400, headers={})
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    return anthropic.BadRequestError(f"Error code: 400 - {body}", response=response, body=body)


class FakeCorpus:
    documents: list = []

    def __len__(self) -> int:
        return 1


def make_engine(side_effect) -> answer.AnswerEngine:
    engine = answer.AnswerEngine(make_config(), FakeCorpus())
    engine._call_api = mock.Mock(side_effect=side_effect)
    return engine


class AnswerEngineTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(answer.doctor, "record_last_error")
        self.record_last_error = patcher.start()
        self.addCleanup(patcher.stop)
        build = mock.patch.object(answer, "build_static_block", return_value="static")
        build.start()
        self.addCleanup(build.stop)

    def test_prompt_too_long_raises_library_too_large_with_detail(self):
        engine = make_engine(bad_request(TOO_LONG))
        with self.assertRaises(answer.LibraryTooLarge) as ctx:
            asyncio.run(engine.answer("How long is probation?"))
        self.assertEqual(ctx.exception.detail, TOO_LONG)
        self.assertIn(TOO_LONG, self.record_last_error.call_args.args[0])

    def test_other_bad_request_still_returns_generic_message(self):
        engine = make_engine(bad_request("max_tokens: must be positive"))
        self.assertEqual(asyncio.run(engine.answer("q")), answer.API_UNREACHABLE_MESSAGE)

    def test_network_error_still_returns_generic_message(self):
        engine = make_engine(RuntimeError("connection reset"))
        self.assertEqual(asyncio.run(engine.answer("q")), answer.API_UNREACHABLE_MESSAGE)


class ReplyFlowTests(unittest.TestCase):
    def make_state(self, answer_side_effect):
        state = mock.Mock()
        state.config = make_config()
        state.corpus = FakeCorpus()
        state.admin_alert_sent_at = {}
        state.answer_engine.answer = mock.AsyncMock(side_effect=answer_side_effect)
        return state

    def ask(self, state, chat_id):
        context = mock.Mock()
        context.bot.send_message = mock.AsyncMock()
        context.bot.send_chat_action = mock.AsyncMock()
        update = mock.Mock()
        update.effective_chat.id = chat_id
        asyncio.run(bot._answer_and_reply(update, context, state, "How long is probation?"))
        return [(c.kwargs["chat_id"], c.kwargs["text"]) for c in context.bot.send_message.call_args_list]

    def test_staff_gets_clear_message_and_admin_gets_the_cause(self):
        state = self.make_state(answer.LibraryTooLarge(TOO_LONG))
        sent = self.ask(state, STAFF_CHAT_ID)
        self.assertEqual(sent[0], (STAFF_CHAT_ID, bot.LIBRARY_TOO_LARGE_STAFF_MESSAGE))
        self.assertEqual(sent[1][0], ADMIN_ID)
        self.assertIn(TOO_LONG, sent[1][1])
        self.assertEqual(len(sent), 2)

    def test_admin_is_alerted_at_most_once_an_hour(self):
        state = self.make_state(answer.LibraryTooLarge(TOO_LONG))
        self.ask(state, STAFF_CHAT_ID)
        sent = self.ask(state, STAFF_CHAT_ID)
        self.assertEqual(sent, [(STAFF_CHAT_ID, bot.LIBRARY_TOO_LARGE_STAFF_MESSAGE)])

        state.admin_alert_sent_at[bot.ALERT_LIBRARY_TOO_LARGE] -= bot.LIBRARY_ALERT_INTERVAL_SECONDS + 1
        sent = self.ask(state, STAFF_CHAT_ID)
        self.assertEqual([chat for chat, _ in sent], [STAFF_CHAT_ID, ADMIN_ID])

    def test_admin_asking_in_their_own_dm_gets_the_cause_once(self):
        state = self.make_state(answer.LibraryTooLarge(TOO_LONG))
        sent = self.ask(state, ADMIN_ID)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], ADMIN_ID)
        self.assertIn(TOO_LONG, sent[0][1])
        self.assertNotEqual(sent[0][1], bot.LIBRARY_TOO_LARGE_STAFF_MESSAGE)

    def test_failed_admin_alert_does_not_break_the_staff_reply(self):
        state = self.make_state(answer.LibraryTooLarge(TOO_LONG))
        context = mock.Mock()
        context.bot.send_chat_action = mock.AsyncMock()
        context.bot.send_message = mock.AsyncMock(side_effect=[None, RuntimeError("chat not found")])
        update = mock.Mock()
        update.effective_chat.id = STAFF_CHAT_ID
        asyncio.run(bot._answer_and_reply(update, context, state, "q"))
        self.assertEqual(context.bot.send_message.await_count, 2)

    def test_normal_answer_is_unchanged(self):
        state = self.make_state(["Answer: 30 minutes."])
        self.assertEqual(self.ask(state, STAFF_CHAT_ID), [(STAFF_CHAT_ID, "Answer: 30 minutes.")])


if __name__ == "__main__":
    unittest.main()
