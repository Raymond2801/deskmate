"""Unit tests for health.py: heartbeat gating and the exit watchdog.

Run from the repo root: .venv/bin/python -m unittest tests.test_health -v

Time is driven by a fake clock, so these run in well under a second, except
the one subprocess test that proves os._exit really ends the process.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram.error import Conflict, NetworkError, TimedOut  # noqa: E402
from telegram.ext import ExtBot  # noqa: E402
from telegram.request import HTTPXRequest  # noqa: E402

import health  # noqa: E402
from bot import HeartbeatBot  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeHttpClient:
    def __init__(self, body: str = "OK") -> None:
        self.ping_times: list[float] = []
        self.clock: FakeClock | None = None
        self.body = body

    async def get(self, url, timeout=None):
        self.ping_times.append(self.clock())
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.text = self.body
        return response


def make_bot(polling_health: health.PollingHealth, get_updates_request=None) -> HeartbeatBot:
    return HeartbeatBot(
        token="123456:TEST-not-a-real-token",
        polling_health=polling_health,
        get_updates_request=get_updates_request or HTTPXRequest(connection_pool_size=1),
    )


async def poll_once(bot: HeartbeatBot) -> None:
    """One PTB polling cycle: errors are swallowed the way network_retry_loop
    swallows them."""
    try:
        await bot.get_updates(timeout=health.POLL_TIMEOUT_SECONDS)
    except NetworkError:
        pass


def run_heartbeat(clock: FakeClock, bot: HeartbeatBot, polling_health, checks: int, on_check=None):
    """Runs heartbeat_loop for `checks` intervals. Each fake sleep advances
    the clock by the interval and runs one poll right before the check."""
    client = FakeHttpClient()
    client.clock = clock
    calls = {"n": 0}

    async def fake_sleep(seconds):
        if calls["n"] == checks:
            raise asyncio.CancelledError
        calls["n"] += 1
        if on_check:
            on_check(calls["n"])
        clock.advance(seconds)
        await poll_once(bot)

    async def main():
        with mock.patch.object(health.asyncio, "sleep", fake_sleep):
            try:
                await health.heartbeat_loop(polling_health, client, "https://hc.example/ping")
            except asyncio.CancelledError:
                pass

    asyncio.run(main())
    return client.ping_times


class HeartbeatBotTests(unittest.TestCase):
    def test_success_is_recorded_only_when_get_updates_returns(self):
        clock = FakeClock()
        polling_health = health.PollingHealth(clock=clock)
        bot = make_bot(polling_health)

        with mock.patch.object(ExtBot, "get_updates", side_effect=NetworkError("Bad Gateway")):
            asyncio.run(poll_once(bot))
        self.assertIsNone(polling_health.last_poll_success)

        with mock.patch.object(ExtBot, "get_updates", return_value=[]):
            asyncio.run(poll_once(bot))
        self.assertEqual(polling_health.last_poll_success, clock.now)


class ConnectionResetTests(unittest.TestCase):
    """After a NetworkError the getUpdates client is rebuilt, so the retry
    can't reuse a keep-alive connection to a bad Telegram frontend."""

    def make_bot_with_fake_request(self):
        request = mock.Mock(spec=HTTPXRequest)
        request.shutdown = mock.AsyncMock()
        request.initialize = mock.AsyncMock()
        return make_bot(health.PollingHealth(clock=FakeClock()), get_updates_request=request), request

    def call_get_updates(self, bot, side_effect):
        async def run():
            with mock.patch.object(ExtBot, "get_updates", side_effect=side_effect):
                return await bot.get_updates(timeout=health.POLL_TIMEOUT_SECONDS)
        return asyncio.run(run())

    def test_bad_gateway_resets_connection_and_still_raises(self):
        bot, request = self.make_bot_with_fake_request()
        with self.assertRaises(NetworkError):
            self.call_get_updates(bot, NetworkError("Bad Gateway"))
        request.shutdown.assert_awaited_once()
        request.initialize.assert_awaited_once()
        self.assertLess(
            request.method_calls.index(mock.call.shutdown()), request.method_calls.index(mock.call.initialize())
        )

    def test_timeout_also_resets_connection(self):
        bot, request = self.make_bot_with_fake_request()
        with self.assertRaises(TimedOut):
            self.call_get_updates(bot, TimedOut())
        request.initialize.assert_awaited_once()

    def test_success_does_not_reset_connection(self):
        bot, request = self.make_bot_with_fake_request()
        self.assertEqual(self.call_get_updates(bot, None), mock.ANY)
        request.shutdown.assert_not_awaited()

    def test_conflict_does_not_reset_connection(self):
        # Conflict (a second poller during a redeploy) is not a network fault.
        bot, request = self.make_bot_with_fake_request()
        with self.assertRaises(Conflict):
            self.call_get_updates(bot, Conflict("terminated by other getUpdates request"))
        request.shutdown.assert_not_awaited()

    def test_failed_reset_does_not_hide_the_original_error(self):
        bot, request = self.make_bot_with_fake_request()
        request.initialize.side_effect = RuntimeError("boom")
        with self.assertRaises(NetworkError):
            self.call_get_updates(bot, NetworkError("Bad Gateway"))

    def test_real_httpx_request_gets_a_new_client(self):
        request = HTTPXRequest(connection_pool_size=1)
        bot = make_bot(health.PollingHealth(clock=FakeClock()), get_updates_request=request)

        async def run():
            await request.initialize()
            old_client = request._client
            with mock.patch.object(ExtBot, "get_updates", side_effect=NetworkError("Bad Gateway")):
                with self.assertRaises(NetworkError):
                    await bot.get_updates()
            new_client = request._client
            self.assertIsNot(new_client, old_client)
            self.assertTrue(old_client.is_closed)
            self.assertFalse(new_client.is_closed)
            await request.shutdown()

        asyncio.run(run())


class BadGatewayTests(unittest.TestCase):
    def test_poll_goes_stale_exactly_after_threshold(self):
        clock = FakeClock()
        polling_health = health.PollingHealth(clock=clock)
        bot = make_bot(polling_health)

        with mock.patch.object(ExtBot, "get_updates", return_value=[]):
            asyncio.run(poll_once(bot))
        last_success = clock.now

        with mock.patch.object(ExtBot, "get_updates", side_effect=NetworkError("Bad Gateway")):
            while clock.now - last_success < health.POLL_STALE_AFTER_SECONDS:
                clock.advance(1)
                asyncio.run(poll_once(bot))
                self.assertIsNone(polling_health.problem())
            clock.advance(1)
            asyncio.run(poll_once(bot))
            self.assertIn("no successful getUpdates", polling_health.problem())

    def test_heartbeat_stops_pinging_once_bad_gateway_starts(self):
        clock = FakeClock()
        polling_health = health.PollingHealth(clock=clock)
        bot = make_bot(polling_health)
        failing = {"on": False}

        async def fake_get_updates(*args, **kwargs):
            if failing["on"]:
                raise NetworkError("Bad Gateway")
            return []

        def on_check(n):
            failing["on"] = n > 2  # checks 1-2 healthy, 3-6 Bad Gateway

        with mock.patch.object(ExtBot, "get_updates", side_effect=fake_get_updates):
            pings = run_heartbeat(clock, bot, polling_health, checks=6, on_check=on_check)

        self.assertEqual(len(pings), 2)

    def test_watchdog_exits_non_zero_after_continuous_bad_gateway(self):
        clock = FakeClock()
        polling_health = health.PollingHealth(clock=clock)
        bot = make_bot(polling_health)
        exit_codes: list[int] = []
        exit_times: list[float] = []

        with mock.patch.object(ExtBot, "get_updates", return_value=[]):
            asyncio.run(poll_once(bot))
        polling_health.evaluate()
        healthy_at = clock.now

        def fake_sleep(seconds):
            # Simulates everything the event loop does meanwhile: polls fail,
            # the monitor evaluates health.
            clock.advance(seconds)
            with mock.patch.object(ExtBot, "get_updates", side_effect=NetworkError("Bad Gateway")):
                asyncio.run(poll_once(bot))
            polling_health.evaluate()
            if exit_codes or clock.now - healthy_at > 3600:
                raise SystemExit  # ends the thread if the watchdog never fires

        def fake_exit(code):
            exit_codes.append(code)
            exit_times.append(clock.now)

        thread = health.start_exit_watchdog(polling_health, exit_fn=fake_exit, sleep_fn=fake_sleep)
        thread.join(timeout=5)

        self.assertEqual(exit_codes, [health.WATCHDOG_EXIT_CODE])
        self.assertNotEqual(exit_codes[0], 0)
        # The exit clock starts once the poll goes stale, so measured from the
        # last successful poll the exit lands at stale + exit-after, give or
        # take one watchdog check on each side.
        self.assertGreater(exit_times[0] - healthy_at, health.WATCHDOG_EXIT_AFTER_SECONDS)
        self.assertLessEqual(
            exit_times[0] - healthy_at,
            health.POLL_STALE_AFTER_SECONDS
            + health.WATCHDOG_EXIT_AFTER_SECONDS
            + 2 * health.WATCHDOG_CHECK_INTERVAL_SECONDS,
        )

    def test_watchdog_really_terminates_process_with_blocked_main_thread(self):
        # Real os._exit in a child process whose main thread is stuck in a
        # blocking sleep, like a blocked event loop. Thresholds shrunk to keep
        # the test fast.
        script = textwrap.dedent(
            """
            import sys, time
            sys.path.insert(0, %r)
            import health
            health.WATCHDOG_CHECK_INTERVAL_SECONDS = 0.05
            health.WATCHDOG_EXIT_AFTER_SECONDS = 0.3
            health.start_exit_watchdog(health.PollingHealth())
            time.sleep(30)
            sys.exit(0)
            """
            % str(REPO_ROOT)
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, health.WATCHDOG_EXIT_CODE)


class HealthyPollingTests(unittest.TestCase):
    def test_empty_successful_polls_keep_heartbeat_pinging(self):
        clock = FakeClock()
        polling_health = health.PollingHealth(clock=clock)
        bot = make_bot(polling_health)

        with mock.patch.object(ExtBot, "get_updates", return_value=[]):
            pings = run_heartbeat(clock, bot, polling_health, checks=6)

        self.assertEqual(len(pings), 6)

    def test_watchdog_does_not_fire_while_polls_succeed(self):
        clock = FakeClock()
        polling_health = health.PollingHealth(clock=clock)
        bot = make_bot(polling_health)
        exit_codes: list[int] = []
        sleeps = {"n": 0}

        def fake_sleep(seconds):
            sleeps["n"] += 1
            if sleeps["n"] > 200:  # 200 x 15s = 50 minutes
                raise SystemExit
            clock.advance(seconds)
            with mock.patch.object(ExtBot, "get_updates", return_value=[]):
                asyncio.run(poll_once(bot))
            polling_health.evaluate()

        thread = health.start_exit_watchdog(polling_health, exit_fn=exit_codes.append, sleep_fn=fake_sleep)
        thread.join(timeout=5)
        self.assertEqual(exit_codes, [])


class DeliveryStallTests(unittest.TestCase):
    """The 2026-09-26 shape: getUpdates succeeds, Telegram holds updates."""

    def test_pending_updates_never_delivered_is_a_problem(self):
        clock = FakeClock()
        polling_health = health.PollingHealth(clock=clock)
        polling_health.record_poll_success([])
        polling_health.record_pending_count(3)
        self.assertIsNone(polling_health.problem())

        clock.advance(health.DELIVERY_CHECK_INTERVAL_SECONDS)
        polling_health.record_poll_success([])
        polling_health.record_pending_count(3)
        self.assertIn("pending update", polling_health.problem())

    def test_pending_updates_that_do_arrive_are_fine(self):
        clock = FakeClock()
        polling_health = health.PollingHealth(clock=clock)
        polling_health.record_pending_count(1)
        clock.advance(health.DELIVERY_CHECK_INTERVAL_SECONDS)
        polling_health.record_poll_success(["an update"])
        polling_health.record_pending_count(1)
        self.assertIsNone(polling_health.problem())

    def test_stall_clears_once_telegram_has_nothing_pending(self):
        clock = FakeClock()
        polling_health = health.PollingHealth(clock=clock)
        for _ in range(3):
            clock.advance(health.DELIVERY_CHECK_INTERVAL_SECONDS)
            polling_health.record_poll_success([])
            polling_health.record_pending_count(2)
        self.assertIsNotNone(polling_health.problem())
        polling_health.record_pending_count(0)
        self.assertIsNone(polling_health.problem())


class HealthcheckResponseTests(unittest.TestCase):
    def run_one_ping(self, body):
        clock = FakeClock()
        polling_health = health.PollingHealth(clock=clock)
        polling_health.record_poll_success([])
        client = FakeHttpClient(body)
        client.clock = clock
        calls = {"n": 0}

        async def fake_sleep(seconds):
            if calls["n"]:
                raise asyncio.CancelledError
            calls["n"] += 1

        async def main():
            with mock.patch.object(health.asyncio, "sleep", fake_sleep):
                try:
                    await health.heartbeat_loop(polling_health, client, "https://hc.example/ping")
                except asyncio.CancelledError:
                    pass

        with self.assertLogs("deskmate.health", level="INFO") as logs:
            asyncio.run(main())
        return logs.output

    def test_unknown_uuid_is_reported_not_logged_as_sent(self):
        output = self.run_one_ping("OK (not found)")
        self.assertTrue(any("not recorded by the server: OK (not found)" in line for line in output))
        self.assertFalse(any("Healthcheck ping sent." in line for line in output))

    def test_plain_ok_is_logged_as_sent(self):
        output = self.run_one_ping("OK")
        self.assertTrue(any("Healthcheck ping sent." in line for line in output))


class QueueStallTests(unittest.TestCase):
    def test_backlog_without_processing_is_a_problem(self):
        clock = FakeClock()
        polling_health = health.PollingHealth(clock=clock)
        polling_health.record_queue_size(4)
        for _ in range(int(health.QUEUE_STALL_AFTER_SECONDS // 10) + 1):
            clock.advance(10)
            polling_health.record_poll_success([])
            polling_health.record_queue_size(4)
        self.assertIn("waiting in the queue", polling_health.problem())

    def test_backlog_that_keeps_draining_is_fine(self):
        clock = FakeClock()
        polling_health = health.PollingHealth(clock=clock)
        for _ in range(30):
            clock.advance(10)
            polling_health.record_poll_success(["u"])
            polling_health.record_queue_size(2)
            polling_health.record_update_processed()
        self.assertIsNone(polling_health.problem())


if __name__ == "__main__":
    unittest.main()
