"""Run bot.py against tests/fake_telegram_server.py and report how the
watchdog behaves. Local only: the bot never talks to Telegram.

Run from the repo root:
  .venv/bin/python tests/run_watchdog_scenario.py bad-gateway-after --seconds 320
  .venv/bin/python tests/run_watchdog_scenario.py pending-not-delivered
  .venv/bin/python tests/run_watchdog_scenario.py bad-gateway-burst

Heartbeat pings go to the fake server's /hc endpoint, or to a real
healthchecks.io check with --real-healthcheck, which reads
TEST_HEALTHCHECK_PING_URL from .env. It refuses to run if that URL points at
the same check as HEALTHCHECK_PING_URL (the production check) in .env or the
environment. Ping URLs and tokens are never printed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [runner] {message}", flush=True)


def read_dotenv() -> dict[str, str]:
    path = REPO_ROOT / ".env"
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def check_identity(url: str) -> str:
    """What identifies a check: host plus path, ignoring scheme, case and a
    trailing slash, so two spellings of the same check compare equal."""
    parsed = urlparse(url.strip())
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/').lower()}"


def resolve_test_ping_url(dotenv: dict[str, str]) -> str:
    test_url = dotenv.get("TEST_HEALTHCHECK_PING_URL", "").strip()
    if not test_url:
        sys.exit("TEST_HEALTHCHECK_PING_URL is not set in .env; refusing to guess a ping URL.")
    production_urls = [
        dotenv.get("HEALTHCHECK_PING_URL", ""),
        os.environ.get("HEALTHCHECK_PING_URL", ""),
    ]
    for production_url in production_urls:
        if production_url.strip() and check_identity(production_url) == check_identity(test_url):
            sys.exit(
                "TEST_HEALTHCHECK_PING_URL points at the same check as HEALTHCHECK_PING_URL "
                "(production). Refusing to run."
            )
    return test_url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["bad-gateway-after", "always-502", "pending-not-delivered", "bad-gateway-burst"])
    parser.add_argument("--seconds", type=float, default=30, help="healthy phase for bad-gateway-after")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--real-healthcheck", action="store_true", help="ping TEST_HEALTHCHECK_PING_URL from .env")
    parser.add_argument("--max-runtime", type=float, default=1200, help="kill the bot after this many seconds")
    args = parser.parse_args()

    if args.real_healthcheck:
        ping_url = resolve_test_ping_url(read_dotenv())
        log("heartbeat target: TEST_HEALTHCHECK_PING_URL from .env (checked: not the production check)")
    else:
        ping_url = f"http://127.0.0.1:{args.port}/hc"
        log("heartbeat target: fake server /hc")

    workdir = Path(tempfile.mkdtemp(prefix="deskmate-watchdog-"))
    stub = subprocess.Popen(
        [PYTHON, str(REPO_ROOT / "tests" / "fake_telegram_server.py"), "--port", str(args.port),
         "--mode", args.mode, "--seconds", str(args.seconds)],
    )
    time.sleep(1)
    if stub.poll() is not None:
        log(f"fake Telegram server exited with code {stub.returncode}; not starting the bot")
        return 1

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", ""),
        "PYTHONUNBUFFERED": "1",
        "TELEGRAM_BOT_TOKEN": "123456:TEST-dummy-not-a-real-token",
        "ANTHROPIC_API_KEY": "sk-test-dummy",
        "COMPANY_NAME": "Watchdog Test Co.",
        "ADMIN_USER_ID": "1",
        "TELEGRAM_API_BASE_URL": f"http://127.0.0.1:{args.port}/bot",
        "HEALTHCHECK_PING_URL": ping_url,
    }
    log(f"starting bot.py (mode={args.mode}, data dir={workdir})")
    started = time.monotonic()
    bot = subprocess.Popen([PYTHON, str(REPO_ROOT / "bot.py")], cwd=workdir, env=env)
    try:
        code = bot.wait(timeout=args.max_runtime)
        log(f"bot.py exited with code {code} after {time.monotonic() - started:.0f}s")
    except subprocess.TimeoutExpired:
        log(f"bot.py still running after {args.max_runtime:.0f}s; sending SIGTERM")
        bot.terminate()
        code = bot.wait(timeout=30)
        log(f"bot.py exited with code {code} after SIGTERM")
    finally:
        stub.terminate()
        stub.wait(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
