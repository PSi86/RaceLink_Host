"""Tests for gateway-status / shutdown helpers (plan P1-1, P1-2)."""

from __future__ import annotations

import unittest

from controller import RaceLink_Host


class _Ui:
    def __init__(self):
        self.notified: list[str] = []

    def message_notify(self, msg: str) -> None:
        self.notified.append(msg)

    def broadcast_ui(self, panel: str) -> None:
        pass


class _Db:
    def __init__(self, initial=None):
        self._store = dict(initial or {})

    def option(self, key, default=None):
        return self._store.get(key, default)

    def option_set(self, key, value):
        self._store[key] = value


class _RhApi:
    def __init__(self, db):
        self.db = db
        self.ui = _Ui()

    def __call__(self, text):
        return text


def _fresh_host():
    return RaceLink_Host(_RhApi(_Db()), "name", "label")


class GatewayStatusTests(unittest.TestCase):
    def test_initial_status_is_not_ready(self):
        host = _fresh_host()
        status = host.gateway_status()
        self.assertFalse(status["ready"])
        self.assertIsNone(status["last_error"])
        self.assertEqual(status["failure_count"], 0)

    def test_record_error_sets_structured_state(self):
        host = _fresh_host()
        host._record_gateway_error(reason="port busy", origin="programmatic")

        status = host.gateway_status()
        self.assertFalse(status["ready"])
        self.assertEqual(status["failure_count"], 1)
        self.assertIsNotNone(status["last_error"])
        self.assertEqual(status["last_error"]["reason"], "port busy")
        self.assertEqual(status["last_error"]["origin"], "programmatic")

    def test_clear_error_resets_counter(self):
        host = _fresh_host()
        host._record_gateway_error(reason="nope", origin="manual")
        host._record_gateway_error(reason="still nope", origin="manual")
        host._clear_gateway_error()

        status = host.gateway_status()
        self.assertEqual(status["failure_count"], 0)
        self.assertIsNone(status["last_error"])

    def test_record_error_escalates_to_error_level_after_three_failures(self):
        host = _fresh_host()

        with self.assertLogs("controller", level="WARNING") as captured:
            host._record_gateway_error(reason="r1", origin="programmatic")
            host._record_gateway_error(reason="r2", origin="programmatic")
            host._record_gateway_error(reason="r3", origin="programmatic")

        levels = [record.levelname for record in captured.records]
        self.assertEqual(levels[:2], ["WARNING", "WARNING"])
        self.assertEqual(levels[2], "ERROR")

    def test_shutdown_is_idempotent_and_flushes_state(self):
        host = _fresh_host()
        host.shutdown()
        self.assertFalse(host.ready)
        # Second call must not raise.
        host.shutdown()


if __name__ == "__main__":
    unittest.main()
