"""The scenario runner must never ping the production healthcheck.

Run from the repo root: .venv/bin/python -m unittest tests.test_scenario_guard -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_watchdog_scenario as runner  # noqa: E402

PROD = "https://hc-ping.com/11111111-2222-3333-4444-555555555555"
TEST = "https://hc-ping.com/66666666-7777-8888-9999-000000000000"


class ScenarioGuardTests(unittest.TestCase):
    def resolve(self, dotenv, environ=None):
        with mock.patch.dict(runner.os.environ, environ or {}, clear=True):
            return runner.resolve_test_ping_url(dotenv)

    def test_distinct_test_url_is_used(self):
        self.assertEqual(self.resolve({"TEST_HEALTHCHECK_PING_URL": TEST, "HEALTHCHECK_PING_URL": PROD}), TEST)

    def test_same_url_as_production_in_dotenv_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            self.resolve({"TEST_HEALTHCHECK_PING_URL": PROD, "HEALTHCHECK_PING_URL": PROD})
        self.assertIn("Refusing", str(ctx.exception))

    def test_same_url_as_production_in_environment_is_refused(self):
        with self.assertRaises(SystemExit):
            self.resolve({"TEST_HEALTHCHECK_PING_URL": PROD}, {"HEALTHCHECK_PING_URL": PROD})

    def test_same_check_spelled_differently_is_refused(self):
        variant = PROD.upper().replace("HTTPS://", "http://") + "/"
        with self.assertRaises(SystemExit):
            self.resolve({"TEST_HEALTHCHECK_PING_URL": variant, "HEALTHCHECK_PING_URL": PROD})

    def test_missing_test_url_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            self.resolve({"HEALTHCHECK_PING_URL": PROD})
        self.assertIn("not set", str(ctx.exception))

    def test_refusal_message_never_contains_the_url(self):
        with self.assertRaises(SystemExit) as ctx:
            self.resolve({"TEST_HEALTHCHECK_PING_URL": PROD, "HEALTHCHECK_PING_URL": PROD})
        self.assertNotIn("11111111", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
