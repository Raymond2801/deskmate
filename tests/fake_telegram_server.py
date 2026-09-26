"""Local stand-in for the Telegram Bot API (and a healthchecks.io ping
endpoint) for exercising the watchdog end to end without touching Telegram.

Point the bot at it with TELEGRAM_API_BASE_URL=http://127.0.0.1:<port>/bot
and HEALTHCHECK_PING_URL=http://127.0.0.1:<port>/hc (or a real
healthchecks.io test check). Any token works; it is never logged.

Modes:
  bad-gateway-after SECONDS  Healthy for SECONDS, then HTTP 502 on every call.
  always-502                 HTTP 502 on every call, including startup.
  pending-not-delivered      getUpdates always returns [] while getWebhookInfo
                             reports pending updates (the 2026-09-26 shape).

Run: .venv/bin/python tests/fake_telegram_server.py --port 8799 --mode bad-gateway-after --seconds 30
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LONG_POLL_SECONDS = 2  # shortened long-poll so the healthy phase isn't idle


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [fake-telegram] {message}", flush=True)


def make_handler(mode: str, healthy_seconds: float, started_at: float):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # silence default access log (it has the token)
            pass

        def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _ok(self, result) -> None:
            self._reply(200, json.dumps({"ok": True, "result": result}).encode())

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)

            if self.path.startswith("/hc"):
                log("HEALTHCHECK PING received")
                self._reply(200, b"OK", "text/plain")
                return

            method = self.path.rstrip("/").rsplit("/", 1)[-1]
            broken = mode == "always-502" or (
                mode == "bad-gateway-after" and time.monotonic() - started_at > healthy_seconds
            )
            if broken:
                log(f"{method} -> 502 Bad Gateway")
                # Same body Telegram sends, so PTB raises NetworkError("Bad Gateway")
                # exactly as in the production log.
                self._reply(502, json.dumps({"ok": False, "error_code": 502, "description": "Bad Gateway"}).encode())
                return

            if method == "getMe":
                self._ok({"id": 123456, "is_bot": True, "first_name": "Deskmate Test", "username": "deskmate_test_bot"})
            elif method == "deleteWebhook":
                self._ok(True)
            elif method == "getWebhookInfo":
                pending = 3 if mode == "pending-not-delivered" else 0
                log(f"getWebhookInfo -> pending_update_count={pending}")
                self._ok({"url": "", "has_custom_certificate": False, "pending_update_count": pending})
            elif method == "getUpdates":
                time.sleep(LONG_POLL_SECONDS)
                self._ok([])
            else:
                self._ok(True)

        do_GET = _handle
        do_POST = _handle

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--mode", choices=["bad-gateway-after", "always-502", "pending-not-delivered"], required=True)
    parser.add_argument("--seconds", type=float, default=30, help="healthy phase for bad-gateway-after")
    args = parser.parse_args()

    handler = make_handler(args.mode, args.seconds, time.monotonic())
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    log(f"listening on 127.0.0.1:{args.port}, mode={args.mode}")
    server.serve_forever()


if __name__ == "__main__":
    main()
