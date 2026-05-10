"""Secondary-discriminator (``expected_key2``) routing in PendingRequestRegistry.

Iteration-3 fix: when two ``OPC_GET_CONFIG`` requests for the same
device but different options are in flight, the registry must wake
each waiter with the reply for *its* option. Without the
discriminator, FIFO matching would route any reply to the oldest
pending waiter and the operator would see "values land in wrong
fields".

This test exercises the registry directly so the routing semantic is
nailed down independently of the gateway-service plumbing.
"""

from __future__ import annotations

import unittest

from racelink.services.pending_requests import (
    RESP_SPECIFIC,
    PendingRequestRegistry,
)


def _reply_event(sender3: bytes, opc: int, *, option: int) -> dict:
    """Build a minimal GET_CONFIG_REPLY-shaped event the registry can match."""
    return {
        "sender3": bytes(sender3),
        "opc": int(opc),
        "reply": "GET_CONFIG_REPLY",
        "option": int(option),
        # data0..3 are not consulted by the registry; the route handler
        # will read them later from req.reply.
        "data0": 0,
        "data1": 0,
        "data2": 0,
        "data3": 0,
    }


class PendingRequestDiscriminatorTests(unittest.TestCase):
    SENDER = bytes.fromhex("DDEEFF")
    OPC_GET_CONFIG = 0x0A

    def test_two_pending_distinct_options_route_to_correct_waiter(self):
        reg = PendingRequestRegistry()
        # Register two waiters for the same (sender, opcode) but with
        # different option discriminators. With FIFO-only matching the
        # FPS waiter would have absorbed the ABL reply (the iteration-3
        # bug). With the discriminator each reply lands at its own waiter.
        req_fps = reg.register(
            sender_last3=self.SENDER,
            expected_key=self.OPC_GET_CONFIG,
            policy=RESP_SPECIFIC,
            timeout_s=1.0,
            expected_key2=0x05,  # FPS
        )
        req_abl = reg.register(
            sender_last3=self.SENDER,
            expected_key=self.OPC_GET_CONFIG,
            policy=RESP_SPECIFIC,
            timeout_s=1.0,
            expected_key2=0x08,  # ABL
        )

        # Reply for ABL should match req_abl, NOT req_fps.
        ev_abl = _reply_event(self.SENDER, self.OPC_GET_CONFIG, option=0x08)
        matched = reg.try_match(ev_abl)
        self.assertIs(matched, req_abl)
        self.assertTrue(req_abl.done.is_set())
        self.assertFalse(req_fps.done.is_set())

        # Subsequent reply for FPS lands on req_fps.
        ev_fps = _reply_event(self.SENDER, self.OPC_GET_CONFIG, option=0x05)
        matched = reg.try_match(ev_fps)
        self.assertIs(matched, req_fps)
        self.assertTrue(req_fps.done.is_set())

    def test_no_match_when_option_byte_disagrees(self):
        reg = PendingRequestRegistry()
        req = reg.register(
            sender_last3=self.SENDER,
            expected_key=self.OPC_GET_CONFIG,
            policy=RESP_SPECIFIC,
            timeout_s=1.0,
            expected_key2=0x05,
        )
        # Reply for a DIFFERENT option must not wake the waiter.
        ev = _reply_event(self.SENDER, self.OPC_GET_CONFIG, option=0x09)
        self.assertIsNone(reg.try_match(ev))
        self.assertFalse(req.done.is_set())
        # Cleanup so the bucket doesn't leak across tests.
        reg.cancel(req)

    def test_legacy_callers_without_discriminator_unchanged(self):
        # Callers that don't pass ``expected_key2`` (the existing
        # OPC_DEVICES / OPC_STATUS / SET_GROUP / CONFIG paths) must
        # continue to FIFO-match on (sender, opcode/ack_of) only.
        reg = PendingRequestRegistry()
        req = reg.register(
            sender_last3=self.SENDER,
            expected_key=self.OPC_GET_CONFIG,
            policy=RESP_SPECIFIC,
            timeout_s=1.0,
        )
        # Any reply with the correct (sender, opcode) wakes it,
        # regardless of the option byte.
        ev = _reply_event(self.SENDER, self.OPC_GET_CONFIG, option=0xAA)
        matched = reg.try_match(ev)
        self.assertIs(matched, req)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
