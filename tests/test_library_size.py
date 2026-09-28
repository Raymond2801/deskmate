"""Unit tests for the post-upload library size and cost report.

Run from the repo root: .venv/bin/python -m unittest tests.test_library_size -v
"""

from __future__ import annotations

import asyncio
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
import library_size  # noqa: E402
from config import Config  # noqa: E402
from library_size import LibrarySize  # noqa: E402

MODEL = "claude-sonnet-4-6"
ADMIN_ID = 111


def make_config(model: str = MODEL) -> Config:
    return Config(
        anthropic_api_key="sk-test",
        telegram_bot_token="123:TEST",
        company_name="Test Co.",
        admin_user_id=ADMIN_ID,
        allowed_chat_ids=frozenset(),
        model=model,
        log_level="INFO",
        healthcheck_ping_url=None,
    )


def size(tokens: int, context: int = 1_000_000, exact: bool = True) -> LibrarySize:
    return LibrarySize(tokens=tokens, tokens_exact=exact, context_tokens=context, context_exact=True)


class CostTests(unittest.TestCase):
    def test_demo_library_matches_the_measured_cost(self):
        # Measured on 2026-09-27: about 5,070 cached tokens for the demo documents,
        # about US$0.020 cold and US$0.0026 warm with ~70 output tokens.
        cold, warm = library_size.question_costs(MODEL, 5_070)
        self.assertAlmostEqual(cold, 5_070 * 3.75e-6 + 100 * 15e-6)
        self.assertAlmostEqual(warm, 5_070 * 0.30e-6 + 100 * 15e-6)
        self.assertTrue(0.019 < cold < 0.022)
        self.assertTrue(0.0025 < warm < 0.0035)

    def test_unknown_model_has_no_cost(self):
        self.assertIsNone(library_size.question_costs("some-future-model", 5_000))

    def test_money_formatting(self):
        self.assertEqual(library_size.format_usd(0.0205), "about US$0.021")
        self.assertEqual(library_size.format_usd(0.0030), "about US$0.003")
        self.assertEqual(library_size.format_usd(0.2935), "about US$0.29")
        self.assertEqual(library_size.format_usd(0.0004), "less than US$0.001")


class ReportTests(unittest.TestCase):
    def test_small_library_has_no_warning(self):
        report = library_size.format_report(size(5_070), MODEL, 8)
        self.assertIn("8 documents, about 5,070 tokens", report)
        self.assertIn("1% of what the AI model can read", report)
        self.assertIn("about US$0.021", report)
        self.assertIn("about US$0.003", report)
        self.assertNotIn("Heads up", report)
        self.assertNotIn("Warning", report)

    def test_cold_cost_over_threshold_gets_heads_up(self):
        # 150,000 tokens: about US$0.56 cold, 15% of the window.
        report = library_size.format_report(size(150_000), MODEL, 20)
        self.assertIn("Heads up", report)
        self.assertIn("/remove", report)
        self.assertNotIn("Warning", report)

    def test_just_under_cost_threshold_has_no_heads_up(self):
        # 130,000 tokens: about US$0.49 cold.
        self.assertNotIn("Heads up", library_size.format_report(size(130_000), MODEL, 20))

    def test_over_80_percent_gets_the_stronger_warning_only(self):
        report = library_size.format_report(size(810_000), MODEL, 40)
        self.assertIn("81%", report)
        self.assertIn("Warning", report)
        self.assertIn("stops answering every question", report)
        self.assertNotIn("Heads up", report)

    def test_context_warning_applies_even_without_prices(self):
        report = library_size.format_report(size(170_000, context=200_000), "some-future-model", 5)
        self.assertNotIn("cost per question", report)
        self.assertIn("Warning", report)

    def test_estimate_is_labelled(self):
        self.assertIn("(rough estimate)", library_size.format_report(size(5_000, exact=False), MODEL, 1))
        self.assertIn("1 document,", library_size.format_report(size(5_000), MODEL, 1))


class MeasureTests(unittest.TestCase):
    def setUp(self):
        library_size._context_window_cache.clear()

    def test_uses_count_tokens_and_models_api(self):
        client = mock.Mock()
        client.messages.count_tokens.return_value = mock.Mock(input_tokens=5_077)
        client.models.retrieve.return_value = mock.Mock(max_input_tokens=1_000_000)
        result = library_size.measure(client, MODEL, "static")
        self.assertEqual(result, LibrarySize(5_077, True, 1_000_000, True))
        # The context window is looked up once per model.
        library_size.measure(client, MODEL, "static")
        client.models.retrieve.assert_called_once()

    def test_falls_back_to_conservative_estimates(self):
        client = mock.Mock()
        client.messages.count_tokens.side_effect = RuntimeError("network down")
        client.models.retrieve.side_effect = RuntimeError("network down")
        block = "x" * 30_000
        result = library_size.measure(client, MODEL, block)
        self.assertEqual(result.tokens, 10_000)
        self.assertFalse(result.tokens_exact)
        self.assertEqual(result.context_tokens, library_size.FALLBACK_CONTEXT_TOKENS)
        self.assertFalse(result.context_exact)

    def test_offline_estimate_overestimates_the_demo_documents(self):
        # The demo static block is 19,581 characters and measured 5,066 tokens.
        self.assertGreater(library_size.estimate_tokens_offline("x" * 19_581), 5_066)


class UploadFlowTests(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._restore)
        for d in (corpus_module.DATA_DIR, corpus_module.DOCS_DIR, corpus_module.ARCHIVE_DIR):
            d.mkdir(parents=True, exist_ok=True)
        # build_static_block reads prompts/system_prompt.md relative to the cwd.
        os.symlink(REPO_ROOT / "prompts", "prompts")
        self.state = bot.BotState(make_config(), corpus_module.Corpus())

    def _restore(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def upload(self, measure):
        update = mock.Mock()
        update.effective_user.id = ADMIN_ID
        update.message.document.file_name = "policy.md"
        update.message.document.file_id = "file-1"
        update.message.reply_text = mock.AsyncMock()
        telegram_file = mock.Mock()
        telegram_file.download_as_bytearray = mock.AsyncMock(return_value=bytearray(b"# Policy\n\nBreaks are 30 minutes.\n"))
        context = mock.Mock()
        context.bot_data = {"state": self.state}
        context.bot.get_file = mock.AsyncMock(return_value=telegram_file)
        with mock.patch.object(library_size, "measure", side_effect=measure):
            asyncio.run(bot.handle_document(update, context))
        return [c.args[0] for c in update.message.reply_text.call_args_list]

    def test_report_follows_the_unchanged_upload_reply(self):
        replies = self.upload(lambda client, model, block: size(1_300))
        self.assertEqual(replies[0], "Got it: policy.md ingested, 6 words extracted.")
        self.assertIn("1 document, about 1,300 tokens", replies[1])
        self.assertEqual(len(replies), 2)

    def test_report_failure_leaves_the_upload_intact(self):
        def broken(client, model, block):
            raise RuntimeError("unexpected")
        replies = self.upload(broken)
        self.assertEqual(replies, ["Got it: policy.md ingested, 6 words extracted."])
        self.assertIsNotNone(self.state.corpus.get("policy.md"))


if __name__ == "__main__":
    unittest.main()
