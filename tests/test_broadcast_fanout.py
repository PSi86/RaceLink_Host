"""Unit tests for :func:`broadcast_fanout` and
:func:`resolve_broadcast_transports`.

Pins:

  * Fan-out runs workers in parallel — wall-clock is bounded by the
    slowest worker, not by N × slowest.
  * Per-worker exceptions are captured; sibling workers still run
    and return successfully.
  * ``timeout_s`` enforces an upper bound on total fan-out wall-clock;
    a stuck worker is marked ``timed_out`` and the helper returns.
  * ``resolve_broadcast_transports`` honours ``target=None`` as the
    deprecated all-attached default; explicit targets resolve via
    ``transport_for_network``.
"""

from __future__ import annotations

import threading
import time
import unittest

from racelink.transport.broadcast_fanout import (
    broadcast_fanout,
    resolve_broadcast_transports,
)
from racelink.transport.broadcast_target import BroadcastTarget


class _FakeTransport:
    def __init__(self, *, ident_mac, network_id=None):
        self.ident_mac = ident_mac
        self.network_id = network_id


class _FakeController:
    def __init__(self, *, transports, network_to_transport=None):
        self._transports = list(transports)
        self._n2t = dict(network_to_transport or {})

    @property
    def transports(self):
        return list(self._transports)

    def transport_for_network(self, network_id):
        return self._n2t.get(network_id)


class FanoutParallelismTests(unittest.TestCase):

    def test_runs_workers_in_parallel(self):
        # Three workers each sleep 120 ms. Sequential would be ~360 ms;
        # parallel ~120 ms + overhead. Allow generous slack.
        delay_s = 0.12
        transports = [object(), object(), object()]

        def _work(_t):
            time.sleep(delay_s)
            return "done"

        start = time.monotonic()
        results = broadcast_fanout(transports, _work, label="test")
        elapsed = time.monotonic() - start

        self.assertEqual(len(results), 3)
        for r in results:
            self.assertEqual(r.outcome, "done")
            self.assertTrue(r.ok)
        # Wall-clock should be well under 3 × delay; allow 2× delay
        # as a generous CI-friendly upper bound that still proves
        # parallelism (sequential would be ~360 ms).
        self.assertLess(elapsed, 2 * delay_s,
                         f"fan-out took {elapsed:.3f}s, expected ≤{2 * delay_s:.3f}s")

    def test_worker_exception_does_not_abort_siblings(self):
        sentinel: list = []

        def _work(t):
            if getattr(t, "raise_now", False):
                raise RuntimeError("simulated failure")
            sentinel.append(getattr(t, "ident_mac", "?"))
            return "ok"

        t_bad = _FakeTransport(ident_mac="bad")
        t_bad.raise_now = True
        t_good = _FakeTransport(ident_mac="good")

        results = broadcast_fanout([t_bad, t_good], _work, label="test")
        self.assertEqual(len(results), 2)
        self.assertIsNotNone(results[0].error)
        self.assertFalse(results[0].ok)
        self.assertEqual(results[1].outcome, "ok")
        self.assertTrue(results[1].ok)
        # Sibling worker ran to completion.
        self.assertEqual(sentinel, ["good"])

    def test_timeout_marks_slow_workers_without_blocking(self):
        # One worker sleeps far longer than the timeout; the other
        # returns immediately. Helper must return inside the timeout
        # window and mark the slow worker.
        gate = threading.Event()

        def _work(t):
            if getattr(t, "is_slow", False):
                # Block until end-of-test so the join times out cleanly.
                gate.wait(timeout=3.0)
                return "late"
            return "fast"

        slow = _FakeTransport(ident_mac="slow")
        slow.is_slow = True
        fast = _FakeTransport(ident_mac="fast")

        start = time.monotonic()
        results = broadcast_fanout(
            [slow, fast], _work,
            timeout_s=0.2, label="test",
        )
        elapsed = time.monotonic() - start

        # Helper returned inside the timeout window.
        self.assertLess(elapsed, 0.6,
                         f"timeout did not bound wall-clock; took {elapsed:.3f}s")
        self.assertTrue(results[0].timed_out)
        self.assertFalse(results[0].ok)
        self.assertEqual(results[1].outcome, "fast")
        self.assertTrue(results[1].ok)
        # Release the slow worker so the daemon thread exits cleanly.
        gate.set()

    def test_empty_transport_list_returns_empty(self):
        called: list = []
        results = broadcast_fanout(
            [], lambda t: called.append(t), label="test",
        )
        self.assertEqual(results, [])
        self.assertEqual(called, [])

    def test_results_preserve_input_order(self):
        # Workers complete in randomly-different order, but the
        # results list MUST follow input order so callers can map
        # by index.
        delays = {"a": 0.05, "b": 0.0, "c": 0.025}
        order_of_completion: list = []

        def _work(t):
            time.sleep(delays[t.ident_mac])
            order_of_completion.append(t.ident_mac)
            return t.ident_mac

        t_a = _FakeTransport(ident_mac="a")
        t_b = _FakeTransport(ident_mac="b")
        t_c = _FakeTransport(ident_mac="c")
        results = broadcast_fanout([t_a, t_b, t_c], _work, label="test")
        # Output order = input order (a, b, c).
        self.assertEqual([r.outcome for r in results], ["a", "b", "c"])
        # Completion order proves parallelism (b finished before c
        # before a, since their delays are 0 / 0.025 / 0.05).
        self.assertEqual(order_of_completion, ["b", "c", "a"])


class ResolveBroadcastTransportsTests(unittest.TestCase):

    def test_target_none_falls_back_to_all_attached(self):
        t_a = _FakeTransport(ident_mac="GW-A")
        t_b = _FakeTransport(ident_mac="GW-B")
        ctrl = _FakeController(transports=[t_a, t_b])
        out = resolve_broadcast_transports(ctrl, None, label="test")
        self.assertEqual(out, [t_a, t_b])

    def test_target_none_with_no_attached_uses_fallback(self):
        ctrl = _FakeController(transports=[])
        fallback = _FakeTransport(ident_mac="primary")
        out = resolve_broadcast_transports(
            ctrl, None, label="test", fallback_transport=fallback,
        )
        self.assertEqual(out, [fallback])

    def test_target_none_with_no_attached_no_fallback_returns_empty(self):
        ctrl = _FakeController(transports=[])
        out = resolve_broadcast_transports(ctrl, None, label="test")
        self.assertEqual(out, [])

    def test_explicit_target_resolves_via_controller(self):
        t_a = _FakeTransport(ident_mac="GW-A")
        t_b = _FakeTransport(ident_mac="GW-B")
        ctrl = _FakeController(
            transports=[t_a, t_b],
            network_to_transport={"net-a": t_a, "net-b": t_b},
        )
        target = BroadcastTarget.from_ids(["net-a", "net-b"])
        out = resolve_broadcast_transports(ctrl, target, label="test")
        self.assertEqual(out, [t_a, t_b])

    def test_explicit_target_skips_unattached_networks(self):
        t_a = _FakeTransport(ident_mac="GW-A")
        ctrl = _FakeController(
            transports=[t_a],
            network_to_transport={"net-a": t_a, "net-missing": None},
        )
        target = BroadcastTarget.from_ids(["net-a", "net-missing"])
        out = resolve_broadcast_transports(ctrl, target, label="test")
        self.assertEqual(out, [t_a])

    def test_explicit_target_coalesces_duplicate_transports(self):
        t_a = _FakeTransport(ident_mac="GW-A")
        ctrl = _FakeController(
            transports=[t_a],
            network_to_transport={"net-a": t_a, "alias-a": t_a},
        )
        target = BroadcastTarget.from_ids(["net-a", "alias-a"])
        out = resolve_broadcast_transports(ctrl, target, label="test")
        self.assertEqual(out, [t_a])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
