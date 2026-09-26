"""Telegram connectivity health: what the healthchecks.io heartbeat reports,
and the watchdog that exits the process when the bot has gone deaf.

"getUpdates returns without raising" is not enough on its own. On 2026-09-26
getUpdates kept succeeding for 90 minutes after a Bad Gateway while no
message was ever handled, so the old heartbeat kept reporting healthy. The
bot is only healthy when all three hold:

1. getUpdates has succeeded recently (empty results count).
2. Telegram isn't holding updates that getUpdates never hands us
   (getWebhookInfo's pending_update_count, checked on a separate connection).
3. Updates that did arrive are being processed, not piling up in the queue.

When the bot has been unhealthy for WATCHDOG_EXIT_AFTER_SECONDS, a plain
thread (not an asyncio task, so it still fires if the event loop itself is
blocked) logs CRITICAL and hard-exits with a non-zero code so Railway's
restart policy brings up a fresh process. A manual restart is what fixed
the 2026-09-26 incident.

Check 2 can also fire for a reason a restart won't fix. We poll with PTB's
Update.ALL_TYPES, which lags the Bot API (PTB 22.8 knows Bot API 10.0;
Telegram is on 10.3 and has update types PTB doesn't list), so some update
types are filtered out. Telegram doesn't document whether filtered updates
count toward pending_update_count, and it keeps updates for up to 24h. To
avoid a restart loop, a delivery stall gets one restart per episode: a
marker on the data volume records it, and if the stall is back after that
restart the watchdog stays put and leaves the heartbeat down to alert the
owner instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from corpus import DATA_DIR

logger = logging.getLogger("deskmate.health")

POLL_TIMEOUT_SECONDS = 10  # Telegram long-poll timeout passed to run_polling

# Explicit HTTP timeouts for every Telegram request (these match PTB's own
# defaults, pinned here so nothing depends on a library default). For
# getUpdates, PTB adds the long-poll timeout to the read timeout, so one poll
# can take at most READ + POLL_TIMEOUT = 15s before it raises TimedOut.
TELEGRAM_CONNECT_TIMEOUT_SECONDS = 5.0
TELEGRAM_READ_TIMEOUT_SECONDS = 5.0
TELEGRAM_WRITE_TIMEOUT_SECONDS = 5.0
TELEGRAM_POOL_TIMEOUT_SECONDS = 1.0

# One poll cycle is at most 15s (see above). Three cycles without a single
# success rules out one slow long-poll or one transient error plus PTB's
# short retry backoff, while still flagging a real outage within a minute.
POLL_STALE_AFTER_SECONDS = 3 * (POLL_TIMEOUT_SECONDS + TELEGRAM_READ_TIMEOUT_SECONDS)

DELIVERY_CHECK_INTERVAL_SECONDS = 60
DELIVERY_CHECK_TIMEOUT_SECONDS = 20
# Telegram must report pending updates on this many consecutive checks, with
# nothing delivered to us in between, before we call delivery stalled.
PENDING_CHECKS_BEFORE_STALL = 2

QUEUE_CHECK_INTERVAL_SECONDS = 15
QUEUE_STALL_AFTER_SECONDS = 120

HEARTBEAT_INTERVAL_SECONDS = 300
HEALTHCHECK_REQUEST_TIMEOUT_SECONDS = 10

WATCHDOG_CHECK_INTERVAL_SECONDS = 15
WATCHDOG_EXIT_AFTER_SECONDS = 300
WATCHDOG_EXIT_CODE = 3

DELIVERY_STALL_MARKER = DATA_DIR / "delivery_stall_restart"
# Telegram drops undelivered updates after 24h, so a stall older than that
# can't be the same episode.
DELIVERY_STALL_MARKER_MAX_AGE_SECONDS = 24 * 3600
# If evaluate() hasn't run for this long, the event loop is not responding.
EVENT_LOOP_UNRESPONSIVE_AFTER_SECONDS = 4 * QUEUE_CHECK_INTERVAL_SECONDS

PROBLEM_POLL_STALE = "poll_stale"
PROBLEM_DELIVERY_STALL = "delivery_stall"
PROBLEM_QUEUE_STALL = "queue_stall"


class PollingHealth:
    """Connectivity state shared by the bot, the monitor tasks, the
    heartbeat and the exit watchdog. All timestamps come from `clock`
    (monotonic by default) so tests can drive time directly."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        stall_marker: Path = DELIVERY_STALL_MARKER,
    ) -> None:
        self.clock = clock
        self.stall_marker = stall_marker
        self.started_at = clock()
        self.last_poll_success: float | None = None
        self.last_update_received: float | None = None
        self.last_update_processed: float | None = None
        self.last_delivery_check: float | None = None
        self.pending_update_count = 0
        self.pending_streak = 0
        self.queue_size = 0
        self.queue_backlog_since: float | None = None
        # Last time evaluate() found nothing wrong. The exit watchdog reads
        # only this, so a blocked event loop (evaluate() never runs) also
        # counts as unhealthy.
        self.last_healthy = self.started_at
        self.last_evaluated = self.started_at
        self.last_problem: str | None = None
        self.last_problem_kind: str | None = None

    def record_poll_success(self, updates: object) -> None:
        now = self.clock()
        self.last_poll_success = now
        if updates:
            self.last_update_received = now

    def record_update_processed(self) -> None:
        self.last_update_processed = self.clock()

    def record_pending_count(self, count: int) -> None:
        now = self.clock()
        previous_check = self.last_delivery_check
        delivered_since_previous_check = (
            previous_check is not None
            and self.last_update_received is not None
            and self.last_update_received >= previous_check
        )
        if count > 0 and not delivered_since_previous_check:
            self.pending_streak += 1
        else:
            self.pending_streak = 0
        self.pending_update_count = count
        self.last_delivery_check = now

    def record_queue_size(self, size: int) -> None:
        self.queue_size = size
        if size == 0:
            self.queue_backlog_since = None
        elif self.queue_backlog_since is None:
            self.queue_backlog_since = self.clock()

    def problem(self) -> str | None:
        """Why the bot is not healthy right now, or None if it is."""
        found = self._find_problem()
        return found[1] if found else None

    def _find_problem(self) -> tuple[str, str] | None:
        now = self.clock()

        reference = self.last_poll_success if self.last_poll_success is not None else self.started_at
        poll_age = now - reference
        if poll_age > POLL_STALE_AFTER_SECONDS:
            return PROBLEM_POLL_STALE, f"no successful getUpdates for {poll_age:.0f}s"

        if self.pending_streak >= PENDING_CHECKS_BEFORE_STALL:
            return PROBLEM_DELIVERY_STALL, (
                f"Telegram reports {self.pending_update_count} pending update(s) but getUpdates "
                f"delivered none across {self.pending_streak} consecutive checks"
            )

        if self.queue_size > 0 and self.queue_backlog_since is not None:
            progress = self.queue_backlog_since
            if self.last_update_processed is not None:
                progress = max(progress, self.last_update_processed)
            if now - progress > QUEUE_STALL_AFTER_SECONDS:
                return PROBLEM_QUEUE_STALL, (
                    f"{self.queue_size} update(s) waiting in the queue with no processing "
                    f"progress for {now - progress:.0f}s"
                )

        return None

    def stall_restart_already_tried(self) -> bool:
        try:
            age = time.time() - self.stall_marker.stat().st_mtime
        except OSError:
            return False
        return age < DELIVERY_STALL_MARKER_MAX_AGE_SECONDS

    def record_stall_restart(self) -> None:
        try:
            self.stall_marker.parent.mkdir(parents=True, exist_ok=True)
            self.stall_marker.touch()
        except OSError as exc:
            logger.warning("Could not write delivery-stall marker: %s", exc)

    def clear_stall_marker(self) -> None:
        try:
            self.stall_marker.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove delivery-stall marker: %s", exc)

    def evaluate(self) -> str | None:
        """Run problem(), stamp last_healthy when there is none, and log
        transitions between healthy and unhealthy once each."""
        found = self._find_problem()
        kind, problem = found if found else (None, None)
        self.last_evaluated = self.clock()
        if problem is None:
            if self.last_problem is not None:
                logger.info("Telegram connectivity recovered.")
            self.last_healthy = self.clock()
            if self.pending_update_count == 0 and self.last_delivery_check is not None:
                # Telegram has confirmed nothing is waiting: the stall episode
                # is over, so a future stall may restart again.
                self.clear_stall_marker()
        elif self.last_problem is None:
            logger.warning("Telegram connectivity unhealthy: %s", problem)
        self.last_problem = problem
        self.last_problem_kind = kind
        return problem


async def queue_monitor_loop(health: PollingHealth, update_queue: asyncio.Queue) -> None:
    while True:
        health.record_queue_size(update_queue.qsize())
        health.evaluate()
        await asyncio.sleep(QUEUE_CHECK_INTERVAL_SECONDS)


async def delivery_check_loop(health: PollingHealth, bot) -> None:
    """Asks Telegram how many updates it is holding for this bot. getWebhookInfo
    works while polling (url is empty) and goes over the general request
    pool, not the single getUpdates connection."""
    while True:
        await asyncio.sleep(DELIVERY_CHECK_INTERVAL_SECONDS)
        try:
            info = await asyncio.wait_for(bot.get_webhook_info(), timeout=DELIVERY_CHECK_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            # Unknown is not the same as stalled: if Telegram is unreachable,
            # getUpdates fails too and the poll-age check catches it.
            logger.warning("Delivery check (getWebhookInfo) failed: %s", exc)
            continue
        health.record_pending_count(info.pending_update_count or 0)
        health.evaluate()


async def heartbeat_loop(health: PollingHealth, client: httpx.AsyncClient, ping_url: str) -> None:
    """Pings healthchecks.io only while the bot is healthy, so a stuck bot
    shows up as missed pings instead of a green check."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        problem = health.evaluate()
        if problem is not None:
            logger.warning("Skipping healthcheck ping: %s", problem)
            continue
        try:
            response = await client.get(ping_url, timeout=HEALTHCHECK_REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            logger.info("Healthcheck ping sent.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Healthcheck ping failed: %s", type(exc).__name__)


def start_exit_watchdog(
    health: PollingHealth,
    exit_fn: Callable[[int], None] = os._exit,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> threading.Thread:
    """Daemon thread that hard-exits once the bot has been unhealthy for
    WATCHDOG_EXIT_AFTER_SECONDS. os._exit skips interpreter and PTB shutdown
    on purpose: a graceful stop can itself hang on a broken connection, and
    the only goal here is a non-zero exit Railway will restart."""

    def run() -> None:
        holding_logged = False
        while True:
            sleep_fn(WATCHDOG_CHECK_INTERVAL_SECONDS)
            now = health.clock()
            unhealthy_for = now - health.last_healthy
            if unhealthy_for <= WATCHDOG_EXIT_AFTER_SECONDS:
                holding_logged = False
            else:
                loop_responding = now - health.last_evaluated <= EVENT_LOOP_UNRESPONSIVE_AFTER_SECONDS
                delivery_stall = loop_responding and health.last_problem_kind == PROBLEM_DELIVERY_STALL
                if delivery_stall and health.stall_restart_already_tried():
                    if not holding_logged:
                        logger.error(
                            "Delivery stall persists after a restart (%s). Not restarting again: "
                            "a restart didn't clear it, so it may be an update type the bot "
                            "doesn't subscribe to. Healthcheck pings stay off so the owner is "
                            "alerted.",
                            health.last_problem,
                        )
                        holding_logged = True
                    continue
                if delivery_stall:
                    health.record_stall_restart()
                logger.critical(
                    "Telegram connectivity unhealthy for %.0fs (last problem: %s). Exiting with "
                    "code %d so the platform restarts the bot.",
                    unhealthy_for,
                    health.last_problem or "event loop not responding",
                    WATCHDOG_EXIT_CODE,
                )
                for handler in logging.getLogger().handlers:
                    handler.flush()
                exit_fn(WATCHDOG_EXIT_CODE)
                return

    thread = threading.Thread(target=run, name="deskmate-exit-watchdog", daemon=True)
    thread.start()
    return thread
