"""The demo documents must never come back once a library has been used.

Run from the repo root: .venv/bin/python -m unittest tests.test_demo_seeding -v

Each "restart" is a fresh Corpus.load() in the same data directory.
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
from corpus import Corpus  # noqa: E402

ADMIN_ID = 111
STAFF_CHAT_ID = -222
DEMO_FILES = ("demo-handbook.md", "demo-leave.md")


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


class DemoSeedingTests(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._restore)
        corpus_module.DEMO_DOCS_DIR.mkdir(parents=True)
        for name in DEMO_FILES:
            (corpus_module.DEMO_DOCS_DIR / name).write_text(f"# {name}\n\nFictional policy.\n")

    def _restore(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def names(self, corpus: Corpus) -> list[str]:
        return [doc.filename for doc in corpus.documents]

    def test_fresh_install_seeds_demo_without_marking_the_library_started(self):
        corpus = Corpus.load()
        self.assertEqual(self.names(corpus), list(DEMO_FILES))
        self.assertFalse(corpus_module.LIBRARY_STARTED_MARKER.exists())
        # A plain restart keeps the demo documents, as before.
        self.assertEqual(self.names(Corpus.load()), list(DEMO_FILES))

    def test_removing_every_demo_document_then_restarting_stays_empty(self):
        corpus = Corpus.load()
        for name in DEMO_FILES:
            corpus.remove_document(name)
        self.assertTrue(corpus_module.LIBRARY_STARTED_MARKER.exists())
        self.assertEqual(len(Corpus.load()), 0)

    def test_reset_demo_then_restarting_stays_empty(self):
        Corpus.load().reset_demo()
        self.assertEqual(len(Corpus.load()), 0)

    def test_upload_then_remove_everything_then_restarting_stays_empty(self):
        corpus = Corpus.load()
        corpus.add_document("real-policy.md", b"# Real policy\n")
        for name in [doc.filename for doc in corpus.documents]:
            corpus.remove_document(name)
        self.assertEqual(len(Corpus.load()), 0)

    def test_upload_alone_marks_the_library_started(self):
        corpus = Corpus.load()
        corpus.add_document("real-policy.md", b"# Real policy\n")
        self.assertTrue(corpus_module.LIBRARY_STARTED_MARKER.exists())

    def test_existing_deployment_with_real_documents_is_marked_on_load(self):
        # A library built before the marker existed: real documents on disk,
        # no marker. Loading it must mark it, so emptying it later is safe.
        corpus_module.DOCS_DIR.mkdir(parents=True)
        (corpus_module.DOCS_DIR / "real-policy.md").write_text("# Real policy\n")
        corpus_module.CORPUS_FILE.write_text(json.dumps([{
            "filename": "real-policy.md", "extracted_text": "# Real policy\n",
            "ingested_at": "2026-09-01T00:00:00+00:00", "byte_size": 14, "source": "upload",
        }]))
        self.assertFalse(corpus_module.LIBRARY_STARTED_MARKER.exists())
        corpus = Corpus.load()
        self.assertTrue(corpus_module.LIBRARY_STARTED_MARKER.exists())
        corpus.remove_document("real-policy.md")
        self.assertEqual(len(Corpus.load()), 0)

    def test_removing_one_of_several_documents_keeps_the_rest_after_restart(self):
        corpus = Corpus.load()
        corpus.remove_document(DEMO_FILES[0])
        self.assertEqual(self.names(Corpus.load()), [DEMO_FILES[1]])


class EmptyLibraryReplyTests(unittest.TestCase):
    def make_state(self):
        state = mock.Mock()
        state.config = make_config()
        state.corpus = []  # len() == 0
        state.admin_alert_sent_at = {}
        state.answer_engine.answer = mock.AsyncMock()
        return state

    def ask(self, state, chat_id):
        context = mock.Mock()
        context.bot.send_message = mock.AsyncMock()
        context.bot.send_chat_action = mock.AsyncMock()
        update = mock.Mock()
        update.effective_chat.id = chat_id
        asyncio.run(bot._answer_and_reply(update, context, state, "How long is probation?"))
        state.answer_engine.answer.assert_not_awaited()
        return [(c.kwargs["chat_id"], c.kwargs["text"]) for c in context.bot.send_message.call_args_list]

    def test_staff_told_no_documents_and_admin_nudged_once_an_hour(self):
        state = self.make_state()
        sent = self.ask(state, STAFF_CHAT_ID)
        self.assertEqual(sent, [(STAFF_CHAT_ID, bot.NO_DOCUMENTS_MESSAGE), (ADMIN_ID, bot.NO_DOCUMENTS_ADMIN_MESSAGE)])
        self.assertEqual(self.ask(state, STAFF_CHAT_ID), [(STAFF_CHAT_ID, bot.NO_DOCUMENTS_MESSAGE)])

    def test_admin_asking_in_their_dm_gets_upload_instructions(self):
        sent = self.ask(self.make_state(), ADMIN_ID)
        self.assertEqual(sent, [(ADMIN_ID, bot.NO_DOCUMENTS_ADMIN_MESSAGE)])
        self.assertIn("file attachments", bot.NO_DOCUMENTS_ADMIN_MESSAGE)

    def test_empty_and_too_large_alerts_are_rate_limited_separately(self):
        state = self.make_state()
        state.admin_alert_sent_at[bot.ALERT_LIBRARY_TOO_LARGE] = __import__("time").monotonic()
        sent = self.ask(state, STAFF_CHAT_ID)
        self.assertIn((ADMIN_ID, bot.NO_DOCUMENTS_ADMIN_MESSAGE), sent)


class EmptyLibraryDocsCommandTests(unittest.TestCase):
    def run_docs(self, user_id):
        state = mock.Mock()
        state.config = make_config()
        state.corpus = []
        context = mock.Mock()
        context.bot_data = {"state": state}
        update = mock.Mock()
        update.effective_user.id = user_id
        update.message.reply_text = mock.AsyncMock()
        asyncio.run(bot.handle_docs(update, context))
        return update.message.reply_text.call_args.args[0]

    def test_admin_gets_upload_instructions(self):
        self.assertEqual(self.run_docs(ADMIN_ID), bot.NO_DOCUMENTS_ADMIN_MESSAGE)

    def test_staff_still_told_to_contact_their_manager(self):
        self.assertEqual(self.run_docs(333), bot.NO_DOCUMENTS_MESSAGE)


if __name__ == "__main__":
    unittest.main()
