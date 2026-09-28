"""Unit tests for /remove and its confirmation buttons.

Run from the repo root: .venv/bin/python -m unittest tests.test_remove_command -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import bot  # noqa: E402
import corpus as corpus_module  # noqa: E402
from config import Config  # noqa: E402

ADMIN_ID = 111
STAFF_ID = 222


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


class TempDataDir(unittest.TestCase):
    """Each test runs in its own empty working directory, so data/ is a
    throwaway folder and the repo's data/ is never touched."""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._restore)
        corpus_module.DATA_DIR.mkdir()
        corpus_module.DOCS_DIR.mkdir()
        corpus_module.ARCHIVE_DIR.mkdir()
        self.corpus = corpus_module.Corpus()
        for name in ("Handbook.md", "leave policy.md", "rostering.md"):
            self.corpus.add_document(name, f"# {name}\n\nSome policy text for {name}.\n".encode())

    def _restore(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()


class CorpusFindRemoveTests(TempDataDir):
    def test_find_exact_and_case_insensitive(self):
        self.assertEqual(self.corpus.find("Handbook.md"), ["Handbook.md"])
        self.assertEqual(self.corpus.find("  handbook.MD "), ["Handbook.md"])
        self.assertEqual(self.corpus.find("leave policy.md"), ["leave policy.md"])
        self.assertEqual(self.corpus.find("nope.md"), [])

    def test_find_reports_names_that_differ_only_by_case(self):
        self.corpus.add_document("handbook.md", b"# lower-case copy\n")
        self.assertEqual(self.corpus.find("HANDBOOK.MD"), ["Handbook.md", "handbook.md"])
        self.assertEqual(self.corpus.find("handbook.md"), ["handbook.md"])

    def test_remove_archives_the_file_and_updates_the_index(self):
        self.corpus.remove_document("Handbook.md")
        self.assertIsNone(self.corpus.get("Handbook.md"))
        self.assertFalse((corpus_module.DOCS_DIR / "Handbook.md").exists())
        archived = list(corpus_module.ARCHIVE_DIR.iterdir())
        self.assertEqual(len(archived), 1)
        self.assertTrue(archived[0].name.startswith("Handbook."))
        saved = [entry["filename"] for entry in json.loads(corpus_module.CORPUS_FILE.read_text())]
        self.assertNotIn("Handbook.md", saved)
        self.assertEqual(len(saved), 2)

    def test_remove_unknown_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.corpus.remove_document("nope.md")


class RemoveCommandTests(TempDataDir):
    def setUp(self):
        super().setUp()
        self.state = bot.BotState(make_config(), self.corpus)
        self.context = mock.Mock()
        self.context.bot_data = {"state": self.state}

    def send(self, text, user_id=ADMIN_ID):
        update = mock.Mock()
        update.effective_user.id = user_id
        update.message.text = text
        update.message.reply_text = mock.AsyncMock()
        asyncio.run(bot.handle_remove(update, self.context))
        return update.message.reply_text.call_args

    def press(self, callback_data, user_id=ADMIN_ID):
        query = mock.Mock()
        query.from_user.id = user_id
        query.data = callback_data
        query.answer = mock.AsyncMock()
        query.edit_message_text = mock.AsyncMock()
        update = mock.Mock()
        update.callback_query = query
        asyncio.run(bot.handle_remove_button(update, self.context))
        return query

    def ask_to_remove(self, name="Handbook.md"):
        call = self.send(f"/remove {name}")
        buttons = call.kwargs["reply_markup"].inline_keyboard[0]
        return {b.text: b.callback_data for b in buttons}

    def test_non_admin_is_refused(self):
        call = self.send("/remove Handbook.md", user_id=STAFF_ID)
        self.assertEqual(call.args[0], bot.ADMIN_ONLY_MESSAGE)
        self.assertEqual(self.state.pending_removals, {})

    def test_missing_name_shows_usage(self):
        self.assertEqual(self.send("/remove").args[0], bot.REMOVE_USAGE_MESSAGE)
        self.assertEqual(self.send("/remove    ").args[0], bot.REMOVE_USAGE_MESSAGE)

    def test_unknown_name_points_to_docs(self):
        text = self.send("/remove nope.md").args[0]
        self.assertIn("couldn't find", text)
        self.assertIn("/docs", text)

    def test_asks_for_confirmation_before_removing(self):
        call = self.send("/remove handbook.md")
        self.assertIn("Remove Handbook.md", call.args[0])
        self.assertIn("words", call.args[0])
        buttons = {b.text: b.callback_data for b in call.kwargs["reply_markup"].inline_keyboard[0]}
        self.assertEqual(set(buttons), {"Remove", "Cancel"})
        for data in buttons.values():
            self.assertLessEqual(len(data.encode()), 64)  # Telegram's callback_data limit
        self.assertIsNotNone(self.corpus.get("Handbook.md"))

    def test_names_with_spaces_and_bot_mention_work(self):
        call = self.send("/remove@deskmate_test_2_bot leave policy.md")
        self.assertIn("Remove leave policy.md", call.args[0])

    def test_remove_button_removes_the_document(self):
        buttons = self.ask_to_remove()
        query = self.press(buttons["Remove"])
        self.assertIsNone(self.corpus.get("Handbook.md"))
        self.assertEqual(query.edit_message_text.call_args.args[0], "Removed Handbook.md. 2 documents left.")

    def test_cancel_button_keeps_the_document(self):
        buttons = self.ask_to_remove()
        query = self.press(buttons["Cancel"])
        self.assertIsNotNone(self.corpus.get("Handbook.md"))
        self.assertIn("Cancelled", query.edit_message_text.call_args.args[0])

    def test_non_admin_press_is_ignored_and_request_stays_open(self):
        buttons = self.ask_to_remove()
        query = self.press(buttons["Remove"], user_id=STAFF_ID)
        self.assertEqual(query.answer.call_args.args[0], bot.ADMIN_ONLY_MESSAGE)
        self.assertTrue(query.answer.call_args.kwargs.get("show_alert"))
        query.edit_message_text.assert_not_awaited()
        self.assertIsNotNone(self.corpus.get("Handbook.md"))
        # The admin can still confirm afterwards.
        self.press(buttons["Remove"])
        self.assertIsNone(self.corpus.get("Handbook.md"))

    def test_expired_request_does_nothing(self):
        buttons = self.ask_to_remove()
        for pending in self.state.pending_removals.values():
            pending["expires_at"] -= bot.REMOVE_CONFIRM_SECONDS + 1
        query = self.press(buttons["Remove"])
        self.assertIn("expired", query.edit_message_text.call_args.args[0])
        self.assertIsNotNone(self.corpus.get("Handbook.md"))

    def test_double_press_does_not_overwrite_the_result(self):
        buttons = self.ask_to_remove()
        first = self.press(buttons["Remove"])
        self.assertEqual(first.edit_message_text.call_args.args[0], "Removed Handbook.md. 2 documents left.")
        second = self.press(buttons["Remove"])
        second.edit_message_text.assert_not_awaited()
        self.assertEqual(second.answer.call_args.args[0], "Already done.")
        self.assertEqual(len(self.corpus), 2)

    def test_pressing_cancel_after_remove_does_not_overwrite_either(self):
        buttons = self.ask_to_remove()
        self.press(buttons["Remove"])
        second = self.press(buttons["Cancel"])
        second.edit_message_text.assert_not_awaited()
        self.assertIsNone(self.corpus.get("Handbook.md"))

    def test_repeat_press_on_a_cancelled_request_is_ignored(self):
        buttons = self.ask_to_remove()
        self.press(buttons["Cancel"])
        second = self.press(buttons["Remove"])
        second.edit_message_text.assert_not_awaited()
        self.assertIsNotNone(self.corpus.get("Handbook.md"))

    def test_repeat_press_queued_behind_a_slow_removal_is_ignored(self):
        # The reported case: removing a large file takes a moment, and a second
        # press is processed right after the first one finishes.
        buttons = self.ask_to_remove()
        original = self.corpus.remove_document
        presses = []

        def slow_remove(filename):
            presses.append(self.press(buttons["Remove"]))  # arrives mid-removal
            return original(filename)

        with mock.patch.object(self.corpus, "remove_document", side_effect=slow_remove):
            first = self.press(buttons["Remove"])
        self.assertEqual(first.edit_message_text.call_args.args[0], "Removed Handbook.md. 2 documents left.")
        presses[0].edit_message_text.assert_not_awaited()
        self.assertEqual(presses[0].answer.call_args.args[0], "Already done.")

    def test_finished_requests_are_forgotten_after_a_while(self):
        buttons = self.ask_to_remove()
        self.press(buttons["Remove"])
        for token in self.state.finished_removals:
            self.state.finished_removals[token] -= bot.REMOVE_FINISHED_MEMORY_SECONDS + 1
        query = self.press(buttons["Remove"])
        self.assertIn("expired", query.edit_message_text.call_args.args[0])
        self.assertEqual(self.state.finished_removals, {})

    def test_document_removed_meanwhile_is_reported(self):
        buttons = self.ask_to_remove()
        self.corpus.remove_document("Handbook.md")
        query = self.press(buttons["Remove"])
        self.assertEqual(query.edit_message_text.call_args.args[0], "Handbook.md was already removed.")

    def test_removing_the_last_document_says_upload_is_needed(self):
        for name in ("leave policy.md", "rostering.md"):
            self.corpus.remove_document(name)
        buttons = self.ask_to_remove()
        text = self.press(buttons["Remove"]).edit_message_text.call_args.args[0]
        self.assertEqual(
            text, "Removed Handbook.md. 0 documents left. The bot can't answer questions until you upload documents."
        )

    def test_admin_too_large_message_now_points_to_remove(self):
        text = bot.LIBRARY_TOO_LARGE_ADMIN_MESSAGE.format(detail="prompt is too long: 2 tokens > 1 maximum")
        self.assertIn("/remove", text)
        self.assertIn("/docs", text)


if __name__ == "__main__":
    unittest.main()
