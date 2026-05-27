"""Unit tests for the TaskManager cancel-cooperative API.

The non-cancel behaviour (start / snapshot / update / busy_response) is
exercised end-to-end through ``test_web_api_routes.py``; this file pins
just the cooperative-cancel methods added for the firmware-update
modal-lock work.
"""

from __future__ import annotations

import threading
import time
import unittest

from racelink.web.tasks import TaskManager


class _NullMasterState:
    def set(self, **_kwargs):
        return None


def _make_manager(captured_events=None):
    events = captured_events if captured_events is not None else []

    def broadcaster(channel, payload):
        events.append((channel, payload))

    return TaskManager(broadcaster=broadcaster, master_state=_NullMasterState())


class TaskManagerCancelTests(unittest.TestCase):
    def test_request_cancel_returns_false_when_idle(self):
        mgr = _make_manager()
        self.assertFalse(mgr.request_cancel())
        self.assertFalse(mgr.is_cancel_requested())

    def test_request_cancel_sets_flag_and_event(self):
        events: list = []
        mgr = _make_manager(events)
        # Block the worker until we let it through so the cancel call
        # lands while ``state == "running"``.
        gate = threading.Event()
        done_signal = threading.Event()

        def target():
            gate.wait(timeout=2.0)
            done_signal.set()
            return {"ok": True}

        mgr.start("test", target)
        # Wait until snapshot reports running.
        for _ in range(20):
            snap = mgr.snapshot()
            if snap and snap.get("state") == "running":
                break
            time.sleep(0.01)
        self.assertTrue(mgr.is_running())

        self.assertTrue(mgr.request_cancel())
        self.assertTrue(mgr.is_cancel_requested())
        snap = mgr.snapshot()
        self.assertTrue(snap.get("cancel_requested"))

        # The cancel must have been broadcast on the "task" channel.
        cancel_broadcasts = [
            payload for ch, payload in events
            if ch == "task" and payload and payload.get("cancel_requested")
        ]
        self.assertTrue(cancel_broadcasts)

        # Let the worker finish so the manager state cleans up.
        gate.set()
        done_signal.wait(timeout=2.0)
        # Give the runner thread a beat to flip state to "done".
        for _ in range(20):
            if mgr.snapshot().get("state") == "done":
                break
            time.sleep(0.01)
        self.assertEqual(mgr.snapshot().get("state"), "done")

    def test_request_cancel_is_idempotent(self):
        mgr = _make_manager()
        gate = threading.Event()

        def target():
            gate.wait(timeout=2.0)
            return {}

        mgr.start("test", target)
        for _ in range(20):
            if mgr.is_running():
                break
            time.sleep(0.01)
        self.assertTrue(mgr.request_cancel())
        # Second call: still True (still a running task; the flag is
        # sticky for the lifetime of the current task) — the contract
        # is "did we signal a running task?" not "was the flag fresh".
        self.assertTrue(mgr.request_cancel())
        gate.set()

    def test_new_task_resets_cancel_flag(self):
        mgr = _make_manager()

        def quick_target():
            return {}

        mgr.start("first", quick_target)
        # Wait for completion.
        for _ in range(50):
            snap = mgr.snapshot()
            if snap and snap.get("state") != "running":
                break
            time.sleep(0.01)
        # Request cancel on the now-completed task — should be False.
        self.assertFalse(mgr.request_cancel())

        # Start a fresh task; its cancel_requested must default to False.
        gate = threading.Event()

        def blocking():
            gate.wait(timeout=2.0)
            return {}

        mgr.start("second", blocking)
        for _ in range(20):
            if mgr.is_running():
                break
            time.sleep(0.01)
        snap = mgr.snapshot()
        self.assertFalse(snap.get("cancel_requested"))
        self.assertFalse(mgr.is_cancel_requested())
        gate.set()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
