"""Gumroad license check: decides whether this deployment may answer.

The repo is public, so this is a deterrent against casual sharing plus an
off switch for refunded or disputed purchases, not DRM. Two rules matter
more than anything else here:

1. Gumroad being unreachable never locks a bot. Any error that isn't a clear
   answer from Gumroad (timeout, connection error, 429, 5xx, a body that isn't
   the JSON we expect) leaves the current status alone and retries later.
2. A purchase's activation is counted once per data volume. The first call
   that reaches Gumroad with a new key sends increment_uses_count=true; every
   other call sends false, so restarts and redeploys never add to "uses".

State lives in license.json next to the bot's other state files under
DATA_DIR. It stores a sha256 of the key, never the key itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

import corpus

logger = logging.getLogger("deskmate.license")

GUMROAD_PRODUCT_ID = "fLgKYwn4yDwzHG1bt-b99g=="
GUMROAD_VERIFY_URL = "https://api.gumroad.com/v2/licenses/verify"
MAX_ACTIVATIONS = 3
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
# Until Gumroad has answered once for this key, retry sooner.
UNVERIFIED_RETRY_SECONDS = 15 * 60
REQUEST_TIMEOUT_SECONDS = 10

STATUS_ACTIVE = "active"
STATUS_LOCKED = "locked"

REASON_OK = "ok"
REASON_MISSING = "missing"
REASON_INVALID = "invalid"
REASON_REVOKED = "revoked"
REASON_ACTIVATION_LIMIT = "activation_limit"
REASON_UNVERIFIED = "unverified"

LICENSE_FILE_NAME = "license.json"


def license_file() -> Path:
    # Read DATA_DIR at call time, not import time: tests chdir to a temp dir.
    return corpus.DATA_DIR / LICENSE_FILE_NAME


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def mask_key(key: str) -> str:
    if not key:
        return "missing"
    return f"...{key[-4:]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LicenseState:
    key_hash: str
    status: str
    reason: str
    last_check_at: str | None = None
    last_ok_at: str | None = None
    activation_counted: bool = False

    @classmethod
    def fresh(cls, key: str) -> "LicenseState":
        return cls(key_hash=key_hash(key), status=STATUS_LOCKED, reason=REASON_UNVERIFIED)


def load_state(key: str) -> LicenseState:
    """The saved state for this key, or a fresh one. A missing, corrupt or
    half-written file, or one saved for a different key, reads as fresh."""
    path = license_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = LicenseState(
            key_hash=data["key_hash"],
            status=data["status"],
            reason=data["reason"],
            last_check_at=data.get("last_check_at"),
            last_ok_at=data.get("last_ok_at"),
            activation_counted=data["activation_counted"],
        )
        valid = (
            isinstance(state.key_hash, str)
            and state.status in (STATUS_ACTIVE, STATUS_LOCKED)
            and isinstance(state.reason, str)
            and isinstance(state.activation_counted, bool)
            and all(v is None or isinstance(v, str) for v in (state.last_check_at, state.last_ok_at))
        )
    except FileNotFoundError:
        return LicenseState.fresh(key)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Ignoring unreadable %s (%s); starting a fresh license check.", path, type(exc).__name__)
        return LicenseState.fresh(key)
    if not valid:
        logger.warning("Ignoring malformed %s; starting a fresh license check.", path)
        return LicenseState.fresh(key)
    if state.key_hash != key_hash(key):
        logger.info("LICENSE_KEY changed since the last check; starting a fresh license check.")
        return LicenseState.fresh(key)
    return state


def save_state(state: LicenseState) -> None:
    """Write via a temp file and rename, so a crash mid-write never leaves a
    half-written license.json. A failure is logged; the state stays in memory."""
    path = license_file()
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".license.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(state), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except OSError as exc:
        logger.warning("Could not save %s: %s", path, type(exc).__name__)
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


class TransientError(Exception):
    """Gumroad gave no clear answer. Keep the current status, retry later."""


@dataclass(frozen=True)
class VerifyResult:
    valid_key: bool
    revoked: bool = False
    uses: int | None = None


def _is_revoked(purchase: dict) -> bool:
    def flag(name: str) -> bool:
        return purchase.get(name) is True

    return flag("refunded") or flag("chargebacked") or (flag("disputed") and not flag("dispute_won"))


async def verify(client: httpx.AsyncClient, key: str, increment: bool) -> VerifyResult:
    """One call to Gumroad's license verify endpoint. Raises TransientError
    for anything that isn't a clear yes or no."""
    form = {
        "product_id": GUMROAD_PRODUCT_ID,
        "license_key": key,
        "increment_uses_count": "true" if increment else "false",
    }
    try:
        response = await asyncio.wait_for(
            client.post(GUMROAD_VERIFY_URL, data=form, timeout=REQUEST_TIMEOUT_SECONDS),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        raise TransientError(type(exc).__name__) from None

    try:
        body = response.json()
    except ValueError:
        body = None

    if response.status_code == 404:
        if isinstance(body, dict) and body.get("success") is False:
            return VerifyResult(valid_key=False)
        raise TransientError("HTTP 404 without a JSON license answer")
    if response.status_code != 200:
        raise TransientError(f"HTTP {response.status_code}")
    if not isinstance(body, dict) or body.get("success") is not True or not isinstance(body.get("purchase"), dict):
        raise TransientError("unexpected response body")

    uses = body.get("uses")
    if isinstance(uses, bool) or not isinstance(uses, int):
        uses = None
    return VerifyResult(valid_key=True, revoked=_is_revoked(body["purchase"]), uses=uses)


class LicenseManager:
    """Holds the current license status. check() is the only thing that
    talks to Gumroad; everything else just reads `state`."""

    def __init__(self, key: str, now: Callable[[], str] = _now_iso) -> None:
        self.key = (key or "").strip()
        self.client: httpx.AsyncClient | None = None
        self._now = now
        self._lock = asyncio.Lock()
        if self.key:
            self.state = load_state(self.key)
        else:
            self.state = LicenseState(key_hash="", status=STATUS_LOCKED, reason=REASON_MISSING)

    @property
    def is_active(self) -> bool:
        return self.state.status == STATUS_ACTIVE

    @property
    def lock_reason(self) -> str | None:
        return None if self.is_active else self.state.reason

    def next_check_delay(self) -> float:
        if self.key and self.state.last_check_at is None:
            return UNVERIFIED_RETRY_SECONDS
        return CHECK_INTERVAL_SECONDS

    def status_line(self) -> str:
        if self.is_active:
            return "active"
        return f"locked ({self.state.reason})"

    async def check(self) -> str:
        """Ask Gumroad once and update the status. Returns a short summary
        for logs and /license. Never raises for a Gumroad problem."""
        async with self._lock:
            return await self._check()

    async def _check(self) -> str:
        if not self.key:
            self.state = LicenseState(key_hash="", status=STATUS_LOCKED, reason=REASON_MISSING)
            logger.info("License check: locked (missing): LICENSE_KEY is not set.")
            return "LICENSE_KEY is not set"
        if self.client is None:
            raise RuntimeError("LicenseManager.client is not set")

        increment = not self.state.activation_counted
        try:
            result = await verify(self.client, self.key, increment)
        except TransientError as exc:
            logger.warning(
                "License check: no clear answer from Gumroad (%s); keeping status %s, retrying later.",
                exc,
                self.status_line(),
            )
            return f"Could not get a clear answer from Gumroad ({exc}). Status unchanged."

        state = self.state
        if result.valid_key and increment:
            # Gumroad counted this activation, whatever happens below.
            state.activation_counted = True
        was_activation_limited = state.status == STATUS_LOCKED and state.reason == REASON_ACTIVATION_LIMIT
        needs_limit = result.valid_key and not result.revoked and (increment or was_activation_limited)
        if needs_limit and result.uses is None:
            # Can't apply the limit without "uses": an unclear answer, so the
            # status stays as it is. Only the counted activation is saved.
            if increment:
                save_state(state)
            logger.warning(
                "License check: Gumroad answered without a usable \"uses\"; keeping status %s.", self.status_line()
            )
            return "Gumroad's answer was missing the activation count. Status unchanged."

        now = self._now()
        state.last_check_at = now
        if not result.valid_key:
            state.status, state.reason = STATUS_LOCKED, REASON_INVALID
        else:
            if result.revoked:
                state.status, state.reason = STATUS_LOCKED, REASON_REVOKED
            elif needs_limit:
                if result.uses <= MAX_ACTIVATIONS:
                    state.status, state.reason = STATUS_ACTIVE, REASON_OK
                else:
                    state.status, state.reason = STATUS_LOCKED, REASON_ACTIVATION_LIMIT
            else:
                # Activated earlier on this volume: later activations elsewhere
                # must not lock this one, so "uses" is ignored here.
                state.status, state.reason = STATUS_ACTIVE, REASON_OK
        if state.status == STATUS_ACTIVE:
            state.last_ok_at = now
        save_state(state)
        logger.info(
            "License check: %s (%s)%s.",
            state.status,
            state.reason,
            ", activation counted" if increment and result.valid_key else "",
        )
        return f"Checked with Gumroad: {self.status_line()}."


async def check_loop(manager: LicenseManager, after_check: Callable[[], object]) -> None:
    """Re-check every CHECK_INTERVAL_SECONDS (sooner while unverified). Any
    error is logged and the loop carries on; cancel it to stop."""
    while True:
        await asyncio.sleep(manager.next_check_delay())
        try:
            await manager.check()
            await after_check()
        except Exception as exc:  # noqa: BLE001
            logger.warning("License check loop error: %s: %s", type(exc).__name__, exc)
