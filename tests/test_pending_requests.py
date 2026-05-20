"""Tests for the unified PendingMatcher / PendingMatcherRegistry (Option D).

Covers both the historical single-sender unicast path (formerly
``PendingRequestRegistry``) and the new multi-sender / wildcard
collector paths that subsumed the old ``send_and_collect`` listener
chain.
"""

from __future__ import annotations

import threading
import time
import unittest

from racelink.services.pending_requests import (
    PendingMatcher,
    PendingMatcherRegistry,
)
from racelink.transport import LP


class UnicastAckMatcherTests(unittest.TestCase):
    """Single-sender, expected_count=1 — replaces the old unicast-ACK tests."""

    def test_ack_match_completes_matcher(self):
        reg = PendingMatcherRegistry()
        sender = bytes.fromhex("DDEEFF")
        m = PendingMatcher(
            sender_filter=frozenset({sender}),
            expected_ack_of=int(LP.OPC_SET_GROUP),
            expected_count=1,
            max_timeout_s=1.0,
        )
        reg.register(m)
        matched = reg.try_match(
            {
                "opc": LP.OPC_ACK,
                "ack_of": int(LP.OPC_SET_GROUP),
                "ack_status": 0,
                "sender3": sender,
            }
        )
        self.assertIs(matched, m)
        self.assertTrue(m.done)
        self.assertEqual(len(m.collected), 1)
        self.assertEqual(m.collected[0]["ack_of"], int(LP.OPC_SET_GROUP))

    def test_different_sender_does_not_match(self):
        reg = PendingMatcherRegistry()
        m = PendingMatcher(
            sender_filter=frozenset({bytes.fromhex("AAAAAA")}),
            expected_ack_of=int(LP.OPC_SET_GROUP),
            expected_count=1,
            max_timeout_s=1.0,
        )
        reg.register(m)
        matched = reg.try_match(
            {
                "opc": LP.OPC_ACK,
                "ack_of": int(LP.OPC_SET_GROUP),
                "ack_status": 0,
                "sender3": bytes.fromhex("BBBBBB"),
            }
        )
        self.assertIsNone(matched)
        self.assertFalse(m.done)

    def test_wrong_ack_of_does_not_match(self):
        reg = PendingMatcherRegistry()
        sender = bytes.fromhex("DDEEFF")
        m = PendingMatcher(
            sender_filter=frozenset({sender}),
            expected_ack_of=int(LP.OPC_SET_GROUP),
            expected_count=1,
            max_timeout_s=1.0,
        )
        reg.register(m)
        matched = reg.try_match(
            {
                "opc": LP.OPC_ACK,
                "ack_of": int(LP.OPC_CONFIG),  # ACK for something else
                "ack_status": 0,
                "sender3": sender,
            }
        )
        self.assertIsNone(matched)
        self.assertFalse(m.done)

    def test_specific_reply_matches_on_opcode(self):
        reg = PendingMatcherRegistry()
        sender = bytes.fromhex("DDEEFF")
        m = PendingMatcher(
            sender_filter=frozenset({sender}),
            expected_opcode=int(LP.OPC_STATUS),
            expected_count=1,
            max_timeout_s=1.0,
        )
        reg.register(m)
        matched = reg.try_match(
            {
                "opc": LP.OPC_STATUS,
                "reply": "STATUS_REPLY",
                "sender3": sender,
            }
        )
        self.assertIs(matched, m)
        self.assertTrue(m.done)

    def test_cancel_is_idempotent(self):
        reg = PendingMatcherRegistry()
        m = PendingMatcher(
            sender_filter=frozenset({bytes.fromhex("DDEEFF")}),
            expected_ack_of=int(LP.OPC_SET_GROUP),
            expected_count=1,
            max_timeout_s=1.0,
        )
        reg.register(m)
        reg.cancel(m)
        reg.cancel(m)  # must not raise
        self.assertEqual(reg.pending_count(), 0)

    def test_multiple_waiters_each_complete_independently(self):
        reg = PendingMatcherRegistry()
        m1 = PendingMatcher(
            sender_filter=frozenset({bytes.fromhex("111111")}),
            expected_ack_of=int(LP.OPC_SET_GROUP),
            expected_count=1,
            max_timeout_s=1.0,
        )
        m2 = PendingMatcher(
            sender_filter=frozenset({bytes.fromhex("222222")}),
            expected_ack_of=int(LP.OPC_SET_GROUP),
            expected_count=1,
            max_timeout_s=1.0,
        )
        reg.register(m1)
        reg.register(m2)
        reg.try_match(
            {
                "opc": LP.OPC_ACK,
                "ack_of": int(LP.OPC_SET_GROUP),
                "ack_status": 0,
                "sender3": bytes.fromhex("222222"),
            }
        )
        self.assertFalse(m1.done)
        self.assertTrue(m2.done)

    def test_dispatch_unblocks_waiter_from_other_thread(self):
        """End-to-end: waiter blocks on cond, dispatcher sets it in <10 ms."""
        reg = PendingMatcherRegistry()
        sender = bytes.fromhex("DDEEFF")
        m = PendingMatcher(
            sender_filter=frozenset({sender}),
            expected_ack_of=int(LP.OPC_SET_GROUP),
            expected_count=1,
            max_timeout_s=2.0,
        )
        reg.register(m)

        def dispatcher():
            time.sleep(0.02)
            reg.try_match(
                {
                    "opc": LP.OPC_ACK,
                    "ack_of": int(LP.OPC_SET_GROUP),
                    "ack_status": 0,
                    "sender3": sender,
                }
            )

        t = threading.Thread(target=dispatcher)
        t.start()
        t0 = time.monotonic()
        reason = m.wait()
        elapsed = time.monotonic() - t0
        t.join()
        self.assertEqual(reason, "count")
        self.assertLess(elapsed, 0.2)


class MultiSenderCollectorTests(unittest.TestCase):
    """New: N-reply collector behaviour (formerly served by send_and_collect)."""

    def test_collects_acks_from_all_senders_in_set(self):
        reg = PendingMatcherRegistry()
        a = bytes.fromhex("AAAAAA")
        b = bytes.fromhex("BBBBBB")
        c = bytes.fromhex("CCCCCC")
        m = PendingMatcher(
            sender_filter=frozenset({a, b, c}),
            expected_ack_of=int(LP.OPC_STREAM),
            expected_count=3,
            idle_timeout_s=0.5,
            max_timeout_s=2.0,
        )
        reg.register(m)
        for sender3 in (a, b, c):
            reg.try_match(
                {
                    "opc": LP.OPC_ACK,
                    "ack_of": int(LP.OPC_STREAM),
                    "ack_status": 0,
                    "sender3": sender3,
                }
            )
        self.assertTrue(m.done)
        self.assertEqual(len(m.collected), 3)
        senders_collected = {bytes(e["sender3"]) for e in m.collected}
        self.assertEqual(senders_collected, {a, b, c})

    def test_idle_timeout_after_first_match(self):
        reg = PendingMatcherRegistry()
        a = bytes.fromhex("AAAAAA")
        b = bytes.fromhex("BBBBBB")
        m = PendingMatcher(
            sender_filter=frozenset({a, b}),
            expected_ack_of=int(LP.OPC_STREAM),
            expected_count=2,  # we'll only deliver 1
            idle_timeout_s=0.1,
            max_timeout_s=2.0,
        )
        reg.register(m)
        # Deliver 1 match in a background thread, then wait quietly.
        def dispatcher():
            time.sleep(0.02)
            reg.try_match(
                {
                    "opc": LP.OPC_ACK,
                    "ack_of": int(LP.OPC_STREAM),
                    "ack_status": 0,
                    "sender3": a,
                }
            )
        t = threading.Thread(target=dispatcher)
        t.start()
        t0 = time.monotonic()
        reason = m.wait()
        elapsed = time.monotonic() - t0
        t.join()
        self.assertEqual(reason, "idle")
        # 0.02s before match + 0.1s idle = ~0.12s; allow generous slack.
        self.assertLess(elapsed, 1.0)
        self.assertGreater(elapsed, 0.10)
        self.assertEqual(len(m.collected), 1)

    def test_no_reply_within_max_timeout(self):
        reg = PendingMatcherRegistry()
        m = PendingMatcher(
            sender_filter=frozenset({bytes.fromhex("DDEEFF")}),
            expected_ack_of=int(LP.OPC_STREAM),
            expected_count=1,
            idle_timeout_s=0.5,
            max_timeout_s=0.1,
        )
        reg.register(m)
        reason = m.wait()
        self.assertEqual(reason, "no_reply")
        self.assertEqual(len(m.collected), 0)

    def test_outside_set_sender_does_not_match(self):
        reg = PendingMatcherRegistry()
        a = bytes.fromhex("AAAAAA")
        b = bytes.fromhex("BBBBBB")
        c_other = bytes.fromhex("CCCCCC")
        m = PendingMatcher(
            sender_filter=frozenset({a, b}),
            expected_ack_of=int(LP.OPC_STREAM),
            expected_count=2,
            max_timeout_s=0.5,
        )
        reg.register(m)
        # An ACK from a sender outside the set should be dropped.
        matched = reg.try_match(
            {
                "opc": LP.OPC_ACK,
                "ack_of": int(LP.OPC_STREAM),
                "ack_status": 0,
                "sender3": c_other,
            }
        )
        self.assertIsNone(matched)
        self.assertEqual(len(m.collected), 0)


class WildcardSenderTests(unittest.TestCase):
    """Discovery-style: any sender, match-by-opcode only."""

    def test_wildcard_matches_any_sender_with_correct_opcode(self):
        reg = PendingMatcherRegistry()
        m = PendingMatcher(
            sender_filter=None,
            expected_opcode=int(LP.OPC_DEVICES),
            expected_count=10,  # large; we'll exit via idle
            idle_timeout_s=0.1,
            max_timeout_s=2.0,
        )
        reg.register(m)
        for sender_hex in ("111111", "222222", "333333"):
            reg.try_match(
                {
                    "opc": LP.OPC_DEVICES,
                    "reply": "IDENTIFY_REPLY",
                    "sender3": bytes.fromhex(sender_hex),
                }
            )
        self.assertEqual(len(m.collected), 3)
        # Now wait and confirm idle-timeout fires (no further matches).
        reason = m.wait()
        self.assertEqual(reason, "idle")

    def test_wildcard_rejects_wrong_opcode(self):
        reg = PendingMatcherRegistry()
        m = PendingMatcher(
            sender_filter=None,
            expected_opcode=int(LP.OPC_DEVICES),
            expected_count=1,
            max_timeout_s=0.2,
        )
        reg.register(m)
        matched = reg.try_match(
            {
                "opc": LP.OPC_STATUS,
                "reply": "STATUS_REPLY",
                "sender3": bytes.fromhex("AAAAAA"),
            }
        )
        self.assertIsNone(matched)
        self.assertEqual(len(m.collected), 0)


if __name__ == "__main__":
    unittest.main()
