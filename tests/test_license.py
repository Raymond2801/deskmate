"""Unit tests for the Gumroad license gate.

Run from the repo root: .venv/bin/python -m unittest tests.test_license -v

Gumroad is never called: every request goes to an httpx.MockTransport. The
license key is a fake one.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from telegram import CallbackQuery, Message, User  # noqa: E402
from telegram import Update  # noqa: E402
from telegram.ext import Application  # noqa: E402

import bot  # noqa: E402
import config as config_module  # noqa: E402
import corpus as corpus_module  # noqa: E402
import doctor  # noqa: E402
import health  # noqa: E402
import licensing  # noqa: E402
from config import Config  # noqa: E402

KEY = "AAAAAAAA-BBBBBBBB-CCCCCCCC-DDDDDDDD"
OTHER_KEY = "EEEEEEEE-FFFFFFFF-00000000-11111111"
ADMIN_ID = 111
STAFF_ID = 222
BOT_ID = 999
BOT_USERNAME = "deskmate_test_2_bot"
GROUP_ID = -1001


def make_config(license_key: str = KEY) -> Config:
    return Config(
        anthropic_api_key="sk-test",
        telegram_bot_token="123:TEST",
        company_name="Test Co.",
        admin_user_id=ADMIN_ID,
        allowed_chat_ids=frozenset(),
        model="claude-sonnet-4-6",
        log_level="INFO",
        healthcheck_ping_url=None,
        license_key=license_key,
    )


def purchase(**flags) -> dict:
    base = {"refunded": False, "disputed": False, "dispute_won": False, "chargebacked": False}
    base.update(flags)
    return base


def ok_body(uses: int = 1, **flags) -> dict:
    return {"success": True, "uses": uses, "purchase": purchase(**flags)}


class FakeGumroad:
    """Answers each verify call with the next queued response and records
    the form fields that were sent."""

    def __init__(self):
        self.responses: list = []
        self.requests: list[dict] = []

    def queue(self, *responses):
        self.responses.extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == licensing.GUMROAD_VERIFY_URL
        self.requests.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return response
        status, body = response
        return httpx.Response(status, json=body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    @property
    def increments(self) -> list[str]:
        return [r["increment_uses_count"] for r in self.requests]


class TempDataDir(unittest.TestCase):
    """Each test runs in its own empty working directory, so data/ is a
    throwaway folder and the repo's data/ is never touched."""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._restore)
        self.gumroad = FakeGumroad()

    def _restore(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def manager(self, key: str = KEY) -> licensing.LicenseManager:
        manager = licensing.LicenseManager(key)
        manager.client = self.gumroad.client()
        return manager

    def check(self, manager: licensing.LicenseManager) -> str:
        return asyncio.run(manager.check())

    def saved(self) -> dict:
        return json.loads(licensing.license_file().read_text())


class VerdictTests(TempDataDir):
    def test_fresh_activation_counts_once_and_unlocks(self):
        self.gumroad.queue((200, ok_body(uses=1)))
        manager = self.manager()
        self.assertEqual(manager.lock_reason, licensing.REASON_UNVERIFIED)
        self.check(manager)
        self.assertTrue(manager.is_active)
        self.assertEqual(self.gumroad.increments, ["true"])
        sent = self.gumroad.requests[0]
        self.assertEqual(sent["product_id"], "fLgKYwn4yDwzHG1bt-b99g==")
        self.assertEqual(sent["license_key"], KEY)
        saved = self.saved()
        self.assertTrue(saved["activation_counted"])
        self.assertEqual(saved["status"], "active")
        self.assertEqual(saved["key_hash"], licensing.key_hash(KEY))
        self.assertIsNotNone(saved["last_ok_at"])
        self.assertNotIn(KEY, licensing.license_file().read_text())

    def test_key_is_stripped(self):
        self.gumroad.queue((200, ok_body()))
        manager = self.manager(f"  {KEY}\n")
        self.check(manager)
        self.assertEqual(self.gumroad.requests[0]["license_key"], KEY)

    def test_activation_over_the_limit_locks(self):
        self.gumroad.queue((200, ok_body(uses=licensing.MAX_ACTIVATIONS + 1)))
        manager = self.manager()
        self.check(manager)
        self.assertEqual(manager.lock_reason, licensing.REASON_ACTIVATION_LIMIT)
        self.assertTrue(self.saved()["activation_counted"])

    def test_activation_at_the_limit_is_fine(self):
        self.gumroad.queue((200, ok_body(uses=licensing.MAX_ACTIVATIONS)))
        manager = self.manager()
        self.check(manager)
        self.assertTrue(manager.is_active)

    def test_404_json_is_invalid(self):
        self.gumroad.queue((404, {"success": False, "message": "That license does not exist for the provided product."}))
        manager = self.manager()
        self.check(manager)
        self.assertEqual(manager.lock_reason, licensing.REASON_INVALID)
        self.assertFalse(self.saved()["activation_counted"])

    def test_404_not_json_is_transient(self):
        self.gumroad.queue(httpx.Response(404, text="<html>Not Found</html>"))
        manager = self.manager()
        self.check(manager)
        self.assertEqual(manager.lock_reason, licensing.REASON_UNVERIFIED)
        self.assertFalse(licensing.license_file().exists())

    def test_404_json_without_success_false_is_transient(self):
        self.gumroad.queue((404, {"error": "route not found"}))
        manager = self.manager()
        self.check(manager)
        self.assertEqual(manager.lock_reason, licensing.REASON_UNVERIFIED)

    def test_refunded_is_revoked(self):
        self.gumroad.queue((200, ok_body(refunded=True)))
        manager = self.manager()
        self.check(manager)
        self.assertEqual(manager.lock_reason, licensing.REASON_REVOKED)

    def test_chargebacked_is_revoked(self):
        self.gumroad.queue((200, ok_body(chargebacked=True)))
        manager = self.manager()
        self.check(manager)
        self.assertEqual(manager.lock_reason, licensing.REASON_REVOKED)

    def test_lost_dispute_is_revoked(self):
        self.gumroad.queue((200, ok_body(disputed=True, dispute_won=False)))
        manager = self.manager()
        self.check(manager)
        self.assertEqual(manager.lock_reason, licensing.REASON_REVOKED)

    def test_won_dispute_is_fine(self):
        self.gumroad.queue((200, ok_body(disputed=True, dispute_won=True)))
        manager = self.manager()
        self.check(manager)
        self.assertTrue(manager.is_active)

    def test_missing_booleans_count_as_false(self):
        self.gumroad.queue((200, {"success": True, "uses": 1, "purchase": {}}))
        manager = self.manager()
        self.check(manager)
        self.assertTrue(manager.is_active)

    def test_sellers_own_test_purchase_is_accepted(self):
        body = ok_body()
        body["purchase"]["test"] = True
        body["purchase"]["email"] = "buyer@example.com"
        self.gumroad.queue((200, body))
        manager = self.manager()
        self.check(manager)
        self.assertTrue(manager.is_active)
        self.assertNotIn("buyer@example.com", licensing.license_file().read_text())

    def test_active_purchase_later_refunded_locks(self):
        self.gumroad.queue((200, ok_body()), (200, ok_body(refunded=True)))
        manager = self.manager()
        self.check(manager)
        self.check(manager)
        self.assertEqual(manager.lock_reason, licensing.REASON_REVOKED)
        self.assertEqual(self.gumroad.increments, ["true", "false"])


TRANSIENT_FAILURES = {
    "connect error": lambda: httpx.ConnectError("refused"),
    "timeout": lambda: httpx.ReadTimeout("slow"),
    "429": lambda: httpx.Response(429, json={"error": "rate limited"}),
    "500": lambda: httpx.Response(500, text="oops"),
    "502": lambda: httpx.Response(502, text="Bad Gateway"),
    "200 not json": lambda: httpx.Response(200, text="<html>maintenance</html>"),
    "200 success false": lambda: httpx.Response(200, json={"success": False}),
    "200 no purchase": lambda: httpx.Response(200, json={"success": True, "uses": 1}),
}


class TransientTests(TempDataDir):
    def test_first_activation_failure_retries_and_writes_nothing(self):
        for name, failure in TRANSIENT_FAILURES.items():
            with self.subTest(name):
                self.gumroad.requests.clear()
                self.gumroad.queue(failure(), (200, ok_body()))
                manager = self.manager()
                self.check(manager)
                self.assertEqual(manager.lock_reason, licensing.REASON_UNVERIFIED)
                self.assertFalse(licensing.license_file().exists())
                self.assertEqual(manager.next_check_delay(), licensing.UNVERIFIED_RETRY_SECONDS)
                self.check(manager)
                self.assertTrue(manager.is_active)
                # The retry is still the activation call.
                self.assertEqual(self.gumroad.increments, ["true", "true"])
                licensing.license_file().unlink()

    def test_later_failure_keeps_active(self):
        for name, failure in TRANSIENT_FAILURES.items():
            with self.subTest(name):
                self.gumroad.queue((200, ok_body()), failure())
                manager = self.manager()
                self.check(manager)
                before = licensing.license_file().read_text()
                self.check(manager)
                self.assertTrue(manager.is_active)
                self.assertEqual(licensing.license_file().read_text(), before)
                self.assertEqual(manager.next_check_delay(), licensing.CHECK_INTERVAL_SECONDS)
                licensing.license_file().unlink()

    def test_later_failure_keeps_locked(self):
        self.gumroad.queue((200, ok_body(refunded=True)), httpx.ConnectError("refused"))
        manager = self.manager()
        self.check(manager)
        self.check(manager)
        self.assertEqual(manager.lock_reason, licensing.REASON_REVOKED)

    def test_request_timeout_is_ten_seconds(self):
        self.assertEqual(licensing.REQUEST_TIMEOUT_SECONDS, 10)

    def test_slow_gumroad_times_out_as_transient(self):
        class Hang(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                await asyncio.sleep(60)

        manager = licensing.LicenseManager(KEY)
        manager.client = httpx.AsyncClient(transport=Hang())
        with mock.patch.object(licensing, "REQUEST_TIMEOUT_SECONDS", 0.05):
            asyncio.run(manager.check())
        self.assertEqual(manager.lock_reason, licensing.REASON_UNVERIFIED)


class VolumeTests(TempDataDir):
    def test_restart_on_same_volume_never_increments(self):
        self.gumroad.queue((200, ok_body(uses=1)))
        self.check(self.manager())
        for _ in range(3):
            # A later activation elsewhere pushed uses past the limit; this
            # deployment activated first and must stay unlocked.
            self.gumroad.queue((200, ok_body(uses=5)))
            manager = self.manager()
            self.assertTrue(manager.is_active)
            self.check(manager)
            self.assertTrue(manager.is_active)
        self.assertEqual(self.gumroad.increments, ["true", "false", "false", "false"])

    def test_changing_the_key_starts_a_new_activation(self):
        self.gumroad.queue((200, ok_body()), (200, ok_body()))
        self.check(self.manager())
        manager = self.manager(OTHER_KEY)
        self.assertEqual(manager.lock_reason, licensing.REASON_UNVERIFIED)
        self.check(manager)
        self.assertEqual(self.gumroad.increments, ["true", "true"])
        self.assertEqual(self.saved()["key_hash"], licensing.key_hash(OTHER_KEY))

    def test_activation_limit_unlocks_when_the_count_drops(self):
        self.gumroad.queue(
            (200, ok_body(uses=4)),
            (200, ok_body(uses=4)),
            (200, ok_body(uses=3)),
        )
        manager = self.manager()
        self.check(manager)
        self.assertEqual(manager.lock_reason, licensing.REASON_ACTIVATION_LIMIT)
        restarted = self.manager()
        self.check(restarted)
        self.assertEqual(restarted.lock_reason, licensing.REASON_ACTIVATION_LIMIT)
        self.check(restarted)
        self.assertTrue(restarted.is_active)
        self.assertEqual(self.gumroad.increments, ["true", "false", "false"])

    def test_invalid_key_unlocks_when_reenabled(self):
        self.gumroad.queue((404, {"success": False}), (200, ok_body(uses=1)))
        manager = self.manager()
        self.check(manager)
        self.assertEqual(manager.lock_reason, licensing.REASON_INVALID)
        self.check(manager)
        self.assertTrue(manager.is_active)
        # The 404 counted nothing, so the next call is the activation.
        self.assertEqual(self.gumroad.increments, ["true", "true"])

    def test_corrupt_or_partial_file_reads_as_fresh(self):
        good = {
            "key_hash": licensing.key_hash(KEY), "status": "active", "reason": "ok",
            "last_check_at": None, "last_ok_at": None, "activation_counted": True,
        }
        bad_contents = [
            "",
            "{",
            json.dumps(good)[:-10],
            "[]",
            "null",
            json.dumps({**good, "status": "great"}),
            json.dumps({**good, "activation_counted": "yes"}),
            json.dumps({k: v for k, v in good.items() if k != "key_hash"}),
            b"\xff\xfe\x00garbage",
        ]
        corpus_module.DATA_DIR.mkdir()
        for content in bad_contents:
            with self.subTest(content=content):
                path = licensing.license_file()
                path.write_bytes(content if isinstance(content, bytes) else content.encode())
                manager = licensing.LicenseManager(KEY)
                self.assertEqual(manager.lock_reason, licensing.REASON_UNVERIFIED)
                self.assertFalse(manager.state.activation_counted)

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        self.gumroad.queue((200, ok_body()))
        self.check(self.manager())
        self.assertEqual(sorted(p.name for p in corpus_module.DATA_DIR.iterdir()), ["license.json"])

    def test_failed_save_keeps_the_old_file(self):
        self.gumroad.queue((200, ok_body()), (200, ok_body(refunded=True)))
        manager = self.manager()
        self.check(manager)
        before = licensing.license_file().read_text()
        with mock.patch.object(licensing.os, "replace", side_effect=OSError("disk full")):
            self.check(manager)
        self.assertEqual(manager.lock_reason, licensing.REASON_REVOKED)
        self.assertEqual(licensing.license_file().read_text(), before)
        self.assertEqual(sorted(p.name for p in corpus_module.DATA_DIR.iterdir()), ["license.json"])

    def test_license_file_sits_beside_the_other_state_files(self):
        self.assertEqual(licensing.license_file(), Path("data") / "license.json")

    def test_documents_never_include_license_json(self):
        corpus = corpus_module.Corpus.load()
        corpus.add_document("Handbook.md", b"# Handbook\n\nText.\n")
        self.gumroad.queue((200, ok_body()))
        self.check(self.manager())
        corpus_module.CORPUS_FILE.unlink()
        reloaded = corpus_module.Corpus.load()
        self.assertEqual([d.filename for d in reloaded.documents], ["Handbook.md"])


class MissingKeyTests(TempDataDir):
    def test_missing_key_locks_without_calling_gumroad(self):
        manager = self.manager("   ")
        self.check(manager)
        self.assertEqual(manager.lock_reason, licensing.REASON_MISSING)
        self.assertEqual(self.gumroad.requests, [])
        self.assertFalse(licensing.license_file().exists())

    def test_config_without_license_key(self):
        config = Config(
            anthropic_api_key="sk-test",
            telegram_bot_token="123:TEST",
            company_name="Test Co.",
            admin_user_id=ADMIN_ID,
            allowed_chat_ids=frozenset(),
            model="claude-sonnet-4-6",
            log_level="INFO",
            healthcheck_ping_url=None,
        )
        self.assertEqual(config.license_key, "")

    def test_load_config_does_not_require_license_key(self):
        environ = {
            "ANTHROPIC_API_KEY": "sk-test",
            "TELEGRAM_BOT_TOKEN": "123:TEST",
            "COMPANY_NAME": "Test Co.",
            "ADMIN_USER_ID": str(ADMIN_ID),
        }
        with mock.patch.dict(os.environ, environ, clear=True):
            self.assertEqual(config_module.load_config().license_key, "")
        with mock.patch.dict(os.environ, {**environ, "LICENSE_KEY": f" {KEY} "}, clear=True):
            self.assertEqual(config_module.load_config().license_key, KEY)


class SecretTests(TempDataDir):
    def test_key_never_reaches_the_logs(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            self.gumroad.queue(
                (200, ok_body()),
                httpx.ConnectError(f"refused for {KEY}"),
                (404, {"success": False}),
                httpx.Response(500, text=KEY),
            )
            manager = self.manager()
            for _ in range(4):
                self.check(manager)
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        output = stream.getvalue()
        self.assertIn("License check", output)
        self.assertNotIn(KEY, output)

    def test_redact_filter_covers_the_license_key(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(bot.RedactSecretsFilter(["123:TEST", "sk-test", "", make_config().license_key]))
        test_logger = logging.getLogger("deskmate.test_license_redact")
        test_logger.addHandler(handler)
        test_logger.propagate = False
        try:
            test_logger.warning("posting license_key=%s", KEY)
        finally:
            test_logger.removeHandler(handler)
        self.assertNotIn(KEY, stream.getvalue())
        self.assertIn("[REDACTED]", stream.getvalue())

    def test_main_adds_the_license_key_to_the_redact_filter(self):
        source = Path(bot.__file__).read_text()
        self.assertIn("config.license_key]", source)

    def test_mask_key_shows_last_four(self):
        self.assertEqual(licensing.mask_key(KEY), "...DDDD")
        self.assertEqual(licensing.mask_key(""), "missing")

    def test_doctor_reports_status_without_the_key(self):
        corpus = corpus_module.Corpus.load()
        state = bot.BotState(make_config(), corpus)
        report = doctor.run_doctor(state.config, corpus, license_status=state.license.status_line())
        self.assertIn("License: locked (unverified)", report)
        self.assertNotIn(KEY, report)


class CheckLoopTests(unittest.TestCase):
    def test_loop_survives_errors_and_cancels_cleanly(self):
        calls = []

        class FlakyManager:
            def next_check_delay(self):
                return 0

            async def check(self):
                calls.append("check")
                if len(calls) == 1:
                    raise RuntimeError("boom")
                return "ok"

        async def after():
            calls.append("after")

        async def run():
            task = asyncio.create_task(licensing.check_loop(FlakyManager(), after))
            while calls.count("check") < 3:
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return task

        with self.assertLogs("deskmate.license", level="WARNING"):
            task = asyncio.run(run())
        self.assertTrue(task.cancelled())
        self.assertEqual(calls[:5], ["check", "check", "after", "check", "after"])


def user(user_id: int) -> dict:
    return {"id": user_id, "is_bot": False, "first_name": "Admin" if user_id == ADMIN_ID else "Staff"}


def private_chat(user_id: int) -> dict:
    return {"id": user_id, "type": "private"}


def text_update(text: str, user_id: int = STAFF_ID, chat: dict | None = None, update_id: int = 1) -> dict:
    message = {
        "message_id": 10,
        "date": 0,
        "chat": chat or private_chat(user_id),
        "from": user(user_id),
        "text": text,
    }
    if text.startswith("/"):
        command = text.split()[0]
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
    return {"update_id": update_id, "message": message}


class GateTests(TempDataDir):
    """Updates go through a real PTB Application with the real handler
    registration; only the handlers behind the gate and Telegram sends are
    mocked."""

    HANDLERS = ("handle_start", "handle_docs", "handle_ask", "handle_doctor", "handle_reset_demo",
                "handle_remove", "handle_license", "handle_remove_button", "handle_document", "handle_message")

    def setUp(self):
        super().setUp()
        self.handlers = {}
        for name in self.HANDLERS:
            patcher = mock.patch.object(bot, name, mock.AsyncMock())
            self.handlers[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.reply_text = mock.AsyncMock()
        self.answer = mock.AsyncMock()
        for target, attr, value in ((Message, "reply_text", self.reply_text), (CallbackQuery, "answer", self.answer)):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.app = Application.builder().token("123:TEST").build()
        self.app.bot._bot_user = User(id=BOT_ID, is_bot=True, first_name="Deskmate", username=BOT_USERNAME)
        self.app._initialized = True  # nothing here talks to Telegram
        self.health = health.PollingHealth()
        self.state = bot.BotState(make_config(), corpus_module.Corpus())
        self.app.bot_data["state"] = self.state
        bot.register_handlers(self.app, self.health)

    def lock(self, reason: str = licensing.REASON_REVOKED):
        self.state.license.state.status = licensing.STATUS_LOCKED
        self.state.license.state.reason = reason

    def unlock(self):
        self.state.license.state.status = licensing.STATUS_ACTIVE
        self.state.license.state.reason = licensing.REASON_OK

    def deliver(self, data: dict):
        self.health.last_update_processed = None
        asyncio.run(self.app.process_update(Update.de_json(data, self.app.bot)))

    def called(self) -> list[str]:
        return [name for name, handler in self.handlers.items() if handler.await_count]

    def replies(self) -> list[str]:
        return [c.args[0] for c in self.reply_text.await_args_list]

    def test_gate_runs_after_the_health_marker(self):
        groups = sorted(self.app.handlers)
        self.assertEqual(groups[:2], [bot.MARK_PROCESSED_GROUP, bot.LICENSE_GATE_GROUP])

    def test_staff_question_gets_the_unavailable_sentence(self):
        self.lock()
        self.deliver(text_update("How long is probation?"))
        self.assertEqual(self.called(), [])
        self.assertEqual(self.replies(), ["Deskmate is not available right now. Please contact your administrator."])
        self.assertIsNotNone(self.health.last_update_processed)

    def test_admin_gets_the_reason(self):
        for reason, text in bot.LICENSE_LOCKED_ADMIN_MESSAGES.items():
            with self.subTest(reason):
                self.reply_text.reset_mock()
                self.lock(reason)
                self.deliver(text_update("How long is probation?", user_id=ADMIN_ID))
                self.assertEqual(self.called(), [])
                self.assertEqual(self.replies(), [text])

    def test_every_admin_command_is_blocked_except_license(self):
        self.lock()
        for command in ("/start", "/docs", "/ask hi", "/doctor", "/reset_demo", "/remove Handbook.md"):
            with self.subTest(command):
                self.deliver(text_update(command, user_id=ADMIN_ID))
                self.assertEqual(self.called(), [])
                self.assertIsNotNone(self.health.last_update_processed)
        self.deliver(text_update("/license", user_id=ADMIN_ID))
        self.assertEqual(self.called(), ["handle_license"])

    def test_admin_license_with_bot_name_passes(self):
        self.lock()
        self.deliver(text_update(f"/license@{BOT_USERNAME}", user_id=ADMIN_ID))
        self.assertEqual(self.called(), ["handle_license"])

    def test_staff_license_is_blocked(self):
        self.lock()
        self.deliver(text_update("/license", user_id=STAFF_ID))
        self.assertEqual(self.called(), [])
        self.assertEqual(self.replies(), [bot.LICENSE_LOCKED_STAFF_MESSAGE])

    def test_document_upload_is_blocked(self):
        self.lock()
        data = text_update("", user_id=ADMIN_ID)
        del data["message"]["text"]
        data["message"]["document"] = {"file_id": "f1", "file_unique_id": "u1", "file_name": "Handbook.md"}
        self.deliver(data)
        self.assertEqual(self.called(), [])
        self.assertEqual(len(self.replies()), 1)
        self.assertIsNotNone(self.health.last_update_processed)

    def test_remove_button_is_blocked_and_answered(self):
        self.lock()
        for user_id, expected in ((ADMIN_ID, bot.LICENSE_LOCKED_ADMIN_BUTTON_MESSAGE),
                                  (STAFF_ID, bot.LICENSE_LOCKED_STAFF_MESSAGE)):
            with self.subTest(user_id):
                self.answer.reset_mock()
                self.deliver({
                    "update_id": 2,
                    "callback_query": {
                        "id": "cb1",
                        "from": user(user_id),
                        "chat_instance": "ci",
                        "data": "rm:abcd1234:yes",
                        "message": {"message_id": 11, "date": 0, "chat": private_chat(ADMIN_ID), "text": "Remove?"},
                    },
                })
                self.assertEqual(self.called(), [])
                self.answer.assert_awaited_once_with(expected, show_alert=True)
                self.assertLessEqual(len(expected), 200)
                self.assertIsNotNone(self.health.last_update_processed)

    def test_admin_addressing_the_bot_in_a_group_gets_only_the_general_sentence(self):
        group = {"id": GROUP_ID, "type": "supergroup", "title": "Staff"}
        mention_text = f"@{BOT_USERNAME} how long is probation?"
        mention = text_update(mention_text, user_id=ADMIN_ID, chat=group)
        mention["message"]["entities"] = [{"type": "mention", "offset": 0, "length": len(BOT_USERNAME) + 1}]
        reply = text_update("and for casuals?", user_id=ADMIN_ID, chat=group)
        reply["message"]["reply_to_message"] = {
            "message_id": 9, "date": 0, "chat": group,
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "Deskmate", "username": BOT_USERNAME},
            "text": "Answer: 6 months.",
        }
        updates = {
            "mention": mention,
            "reply": reply,
            "/ask": text_update("/ask How long is probation?", user_id=ADMIN_ID, chat=group),
            "/doctor": text_update("/doctor", user_id=ADMIN_ID, chat=group),
            "/docs@bot": text_update(f"/docs@{BOT_USERNAME}", user_id=ADMIN_ID, chat=group),
        }
        for reason in bot.LICENSE_LOCKED_ADMIN_MESSAGES:
            self.lock(reason)
            for name, data in updates.items():
                with self.subTest(reason=reason, update=name):
                    self.reply_text.reset_mock()
                    self.deliver(data)
                    self.assertEqual(self.called(), [])
                    self.assertEqual(self.replies(), [bot.LICENSE_LOCKED_STAFF_MESSAGE])
                    sent = " ".join(self.replies())
                    self.assertNotIn(reason, sent)
                    self.assertNotIn("license key", sent)
                    self.assertNotIn("DDDD", sent)
                    self.assertNotIn(KEY, sent)

    def test_admin_private_chat_still_gets_details(self):
        self.lock(licensing.REASON_REVOKED)
        self.deliver(text_update("How long is probation?", user_id=ADMIN_ID))
        self.assertEqual(self.replies(), [bot.LICENSE_LOCKED_ADMIN_MESSAGES[licensing.REASON_REVOKED]])

    def test_admin_license_in_a_group_reaches_the_handler(self):
        # The handler then answers with the private-chat pointer
        # (LicenseCommandTests); the gate itself sends nothing.
        self.lock()
        group = {"id": GROUP_ID, "type": "supergroup", "title": "Staff"}
        self.deliver(text_update("/license", user_id=ADMIN_ID, chat=group))
        self.assertEqual(self.called(), ["handle_license"])
        self.assertEqual(self.replies(), [])

    def test_staff_license_in_a_group_gets_the_general_sentence(self):
        self.lock()
        group = {"id": GROUP_ID, "type": "supergroup", "title": "Staff"}
        self.deliver(text_update("/license", user_id=STAFF_ID, chat=group))
        self.assertEqual(self.called(), [])
        self.assertEqual(self.replies(), [bot.LICENSE_LOCKED_STAFF_MESSAGE])

    def test_admin_button_in_a_group_gets_the_general_sentence(self):
        self.lock()
        group = {"id": GROUP_ID, "type": "supergroup", "title": "Staff"}
        self.deliver({
            "update_id": 4,
            "callback_query": {
                "id": "cb2",
                "from": user(ADMIN_ID),
                "chat_instance": "ci",
                "data": "rm:abcd1234:yes",
                "message": {"message_id": 12, "date": 0, "chat": group, "text": "Remove?"},
            },
        })
        self.assertEqual(self.called(), [])
        self.answer.assert_awaited_once_with(bot.LICENSE_LOCKED_STAFF_MESSAGE, show_alert=True)

    def test_group_chatter_is_blocked_silently(self):
        self.lock()
        group = {"id": GROUP_ID, "type": "supergroup", "title": "Staff"}
        self.deliver(text_update("lunch anyone?", chat=group))
        self.assertEqual(self.called(), [])
        self.assertEqual(self.replies(), [])
        self.assertIsNotNone(self.health.last_update_processed)

    def test_group_command_for_another_bot_is_silent(self):
        self.lock()
        group = {"id": GROUP_ID, "type": "supergroup", "title": "Staff"}
        self.deliver(text_update("/start@other_bot", chat=group))
        self.assertEqual(self.replies(), [])

    def test_group_ask_gets_the_unavailable_sentence(self):
        self.lock()
        group = {"id": GROUP_ID, "type": "supergroup", "title": "Staff"}
        self.deliver(text_update("/ask How long is probation?", chat=group))
        self.assertEqual(self.called(), [])
        self.assertEqual(self.replies(), [bot.LICENSE_LOCKED_STAFF_MESSAGE])

    def test_chat_outside_allowed_chats_is_silent(self):
        self.lock()
        self.state.config = dataclasses.replace(make_config(), allowed_chat_ids=frozenset({-42}))
        self.deliver(text_update("How long is probation?"))
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.called(), [])

    def test_update_without_a_message_is_blocked_and_marked(self):
        self.lock()
        self.deliver({
            "update_id": 3,
            "my_chat_member": {
                "chat": {"id": GROUP_ID, "type": "supergroup", "title": "Staff"},
                "from": user(ADMIN_ID),
                "date": 0,
                "old_chat_member": {"status": "left", "user": {"id": BOT_ID, "is_bot": True, "first_name": "D"}},
                "new_chat_member": {"status": "member", "user": {"id": BOT_ID, "is_bot": True, "first_name": "D"}},
            },
        })
        self.assertEqual(self.replies(), [])
        self.assertIsNotNone(self.health.last_update_processed)

    def test_failed_locked_reply_still_blocks(self):
        self.lock()
        self.reply_text.side_effect = RuntimeError("chat not found")
        self.deliver(text_update("How long is probation?"))
        self.assertEqual(self.called(), [])
        self.assertIsNotNone(self.health.last_update_processed)

    def test_active_license_changes_nothing(self):
        self.unlock()
        self.deliver(text_update("How long is probation?"))
        self.deliver(text_update("/doctor", user_id=ADMIN_ID))
        self.assertEqual(self.called(), ["handle_doctor", "handle_message"])
        self.assertEqual(self.replies(), [])
        self.assertIsNotNone(self.health.last_update_processed)


class AnnounceTests(TempDataDir):
    def setUp(self):
        super().setUp()
        self.state = bot.BotState(make_config(), corpus_module.Corpus())
        self.bot = mock.Mock()
        self.bot.send_message = mock.AsyncMock()

    def announce(self):
        asyncio.run(bot.announce_license_status(self.bot, self.state))

    def set(self, status, reason):
        self.state.license.state.status = status
        self.state.license.state.reason = reason

    def sent(self) -> list[str]:
        return [c.kwargs["text"] for c in self.bot.send_message.await_args_list]

    def test_one_alert_per_change(self):
        self.set(licensing.STATUS_LOCKED, licensing.REASON_REVOKED)
        self.announce()
        self.announce()
        self.assertEqual(self.sent(), [bot.LICENSE_LOCKED_ADMIN_MESSAGES[licensing.REASON_REVOKED]])
        self.assertEqual(self.bot.send_message.await_args.kwargs["chat_id"], ADMIN_ID)

        self.set(licensing.STATUS_LOCKED, licensing.REASON_INVALID)
        self.announce()
        self.assertEqual(len(self.sent()), 2)

        self.set(licensing.STATUS_ACTIVE, licensing.REASON_OK)
        self.announce()
        self.announce()
        self.assertEqual(len(self.sent()), 2)

        # Locking again soon after an unlock still alerts (no hourly throttle).
        self.set(licensing.STATUS_LOCKED, licensing.REASON_INVALID)
        self.announce()
        self.assertEqual(len(self.sent()), 3)

    def test_starting_up_locked_alerts_once(self):
        corpus_module.DATA_DIR.mkdir(exist_ok=True)
        licensing.save_state(licensing.LicenseState(
            key_hash=licensing.key_hash(KEY), status="locked", reason="revoked",
            last_check_at="2026-10-01T00:00:00+00:00", activation_counted=True,
        ))
        state = bot.BotState(make_config(), corpus_module.Corpus())
        self.gumroad.queue(httpx.ConnectError("refused"))
        state.license.client = self.gumroad.client()
        asyncio.run(state.license.check())
        asyncio.run(bot.announce_license_status(self.bot, state))
        asyncio.run(bot.announce_license_status(self.bot, state))
        self.assertEqual(self.sent(), [bot.LICENSE_LOCKED_ADMIN_MESSAGES[licensing.REASON_REVOKED]])

    def test_missing_key_alerts(self):
        state = bot.BotState(make_config(""), corpus_module.Corpus())
        asyncio.run(state.license.check())
        asyncio.run(bot.announce_license_status(self.bot, state))
        self.assertEqual(self.sent(), [bot.LICENSE_LOCKED_ADMIN_MESSAGES[licensing.REASON_MISSING]])


class LicenseCommandTests(TempDataDir):
    def setUp(self):
        super().setUp()
        self.state = bot.BotState(make_config(), corpus_module.Corpus())
        self.state.license.client = self.gumroad.client()
        self.context = mock.Mock()
        self.context.bot_data = {"state": self.state}

    def run_command(self, user_id: int, chat_type: str = "private") -> str:
        update = mock.Mock()
        update.effective_user.id = user_id
        update.effective_chat.type = chat_type
        update.message.reply_text = mock.AsyncMock()
        asyncio.run(bot.handle_license(update, self.context))
        return update.message.reply_text.await_args.args[0]

    def test_staff_cannot_use_it(self):
        self.assertEqual(self.run_command(STAFF_ID), bot.ADMIN_ONLY_MESSAGE)
        self.assertEqual(self.gumroad.requests, [])

    def test_admin_forces_a_check_and_sees_a_masked_key(self):
        self.gumroad.queue((200, ok_body()))
        reply = self.run_command(ADMIN_ID)
        self.assertEqual(len(self.gumroad.requests), 1)
        self.assertIn("License: active", reply)
        self.assertIn("Key: ...DDDD", reply)
        self.assertIn("Last confirmed valid: 20", reply)
        self.assertNotIn(KEY, reply)

    def test_admin_sees_the_reason_when_locked_and_gets_no_extra_alert(self):
        self.gumroad.queue((200, ok_body(refunded=True)))
        reply = self.run_command(ADMIN_ID)
        self.assertIn("License: locked (revoked)", reply)
        self.assertIn(bot.LICENSE_LOCKED_ADMIN_MESSAGES[licensing.REASON_REVOKED], reply)
        bot_mock = mock.Mock()
        bot_mock.send_message = mock.AsyncMock()
        asyncio.run(bot.announce_license_status(bot_mock, self.state))
        bot_mock.send_message.assert_not_awaited()

    def test_admin_license_in_a_group_points_to_a_private_chat(self):
        for locked in (False, True):
            with self.subTest(locked=locked):
                self.state.license.state.status = licensing.STATUS_LOCKED if locked else licensing.STATUS_ACTIVE
                self.state.license.state.reason = licensing.REASON_REVOKED if locked else licensing.REASON_OK
                for chat_type in ("group", "supergroup"):
                    reply = self.run_command(ADMIN_ID, chat_type=chat_type)
                    self.assertEqual(reply, "Please send /license to me in a private chat.")
                self.assertEqual(self.gumroad.requests, [])

    def test_staff_license_in_a_group_keeps_the_existing_reply(self):
        self.assertEqual(self.run_command(STAFF_ID, chat_type="supergroup"), bot.ADMIN_ONLY_MESSAGE)
        self.assertEqual(self.gumroad.requests, [])

    def test_doctor_in_a_group_hides_the_lock_reason(self):
        self.state.license.state.status = licensing.STATUS_LOCKED
        self.state.license.state.reason = licensing.REASON_REVOKED
        reports = {}
        for chat_type in ("private", "supergroup"):
            update = mock.Mock()
            update.effective_user.id = ADMIN_ID
            update.effective_chat.type = chat_type
            update.message.reply_text = mock.AsyncMock()
            asyncio.run(bot.handle_doctor(update, self.context))
            reports[chat_type] = update.message.reply_text.await_args.args[0]
        self.assertIn("License: locked (revoked)", reports["private"])
        self.assertIn("License: locked\n", reports["supergroup"])
        self.assertNotIn("revoked", reports["supergroup"])
        self.assertNotIn("DDDD", reports["supergroup"])

    def test_gumroad_down_is_reported_not_fatal(self):
        self.gumroad.queue(httpx.ConnectError("refused"))
        reply = self.run_command(ADMIN_ID)
        self.assertIn("Could not get a clear answer from Gumroad", reply)
        self.assertIn("License: locked (unverified)", reply)


class CustomerTextTests(unittest.TestCase):
    def test_no_em_dashes_in_license_messages(self):
        texts = [bot.LICENSE_LOCKED_STAFF_MESSAGE, bot.LICENSE_LOCKED_ADMIN_BUTTON_MESSAGE,
                 *bot.LICENSE_LOCKED_ADMIN_MESSAGES.values()]
        for text in texts:
            self.assertNotIn("—", text)
            self.assertNotIn("–", text)


if __name__ == "__main__":
    unittest.main()
