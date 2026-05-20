"""Secondary-discriminator (``discriminator_field``) routing in PendingMatcherRegistry.

Iteration-3 fix (preserved under Option D refactor): when two
``OPC_GET_CONFIG`` requests for the same device but different options
are in flight, the registry must wake each waiter with the reply for
*its* option. Without the discriminator, FIFO matching would route any
reply to the oldest pending waiter and the operator would see "values
land in wrong fields".

The unified ``PendingMatcher`` carries the discriminator as the pair
``(discriminator_field, discriminator_value)`` — today only the
``"option"`` field of GET_CONFIG_REPLY is used, but the mechanism is
generic.
"""

from __future__ import annotations

import unittest

from racelink.services.pending_requests import (
    PendingMatcher,
    PendingMatcherRegistry,
)


def _reply_event(sender3: bytes, opc: int, *, option: int) -> dict:
    """Build a minimal GET_CONFIG_REPLY-shaped event the registry can match."""
    return {
        "sender3": bytes(sender3),
        "opc": int(opc),
        "reply": "GET_CONFIG_REPLY",
        "option": int(option),
        # data0..3 are not consulted by the registry; the route handler
        # will read them later from matcher.collected.
        "data0": 0,
        "data1": 0,
        "data2": 0,
        "data3": 0,
    }


class PendingMatcherDiscriminatorTests(unittest.TestCase):
    SENDER = bytes.fromhex("DDEEFF")
    OPC_GET_CONFIG = 0x0A

    def test_two_pending_distinct_options_route_to_correct_waiter(self):
        reg = PendingMatcherRegistry()
        # Register two waiters for the same (sender, opcode) but with
        # different option discriminators. With FIFO-only matching the
        # FPS waiter would have absorbed the ABL reply (the iteration-3
        # bug). With the discriminator each reply lands at its own waiter.
        m_fps = PendingMatcher(
            sender_filter=frozenset({self.SENDER}),
            expected_opcode=self.OPC_GET_CONFIG,
            discriminator_field="option",
            discriminator_value=0x05,  # FPS
            expected_count=1,
            max_timeout_s=1.0,
        )
        m_abl = PendingMatcher(
            sender_filter=frozenset({self.SENDER}),
            expected_opcode=self.OPC_GET_CONFIG,
            discriminator_field="option",
            discriminator_value=0x08,  # ABL
            expected_count=1,
            max_timeout_s=1.0,
        )
        reg.register(m_fps)
        reg.register(m_abl)

        # Reply for ABL should match m_abl, NOT m_fps.
        ev_abl = _reply_event(self.SENDER, self.OPC_GET_CONFIG, option=0x08)
        matched = reg.try_match(ev_abl)
        self.assertIs(matched, m_abl)
        self.assertTrue(m_abl.done)
        self.assertFalse(m_fps.done)

        # Subsequent reply for FPS lands on m_fps.
        ev_fps = _reply_event(self.SENDER, self.OPC_GET_CONFIG, option=0x05)
        matched = reg.try_match(ev_fps)
        self.assertIs(matched, m_fps)
        self.assertTrue(m_fps.done)

    def test_no_match_when_option_byte_disagrees(self):
        reg = PendingMatcherRegistry()
        m = PendingMatcher(
            sender_filter=frozenset({self.SENDER}),
            expected_opcode=self.OPC_GET_CONFIG,
            discriminator_field="option",
            discriminator_value=0x05,
            expected_count=1,
            max_timeout_s=1.0,
        )
        reg.register(m)
        # Reply for a DIFFERENT option must not wake the waiter.
        ev = _reply_event(self.SENDER, self.OPC_GET_CONFIG, option=0x09)
        self.assertIsNone(reg.try_match(ev))
        self.assertFalse(m.done)
        # Cleanup so the bucket doesn't leak across tests.
        reg.cancel(m)

    def test_legacy_callers_without_discriminator_unchanged(self):
        # Callers that don't set the discriminator (the existing
        # OPC_DEVICES / OPC_STATUS / SET_GROUP / CONFIG paths) must
        # continue to match on (sender, opcode/ack_of) only.
        reg = PendingMatcherRegistry()
        m = PendingMatcher(
            sender_filter=frozenset({self.SENDER}),
            expected_opcode=self.OPC_GET_CONFIG,
            expected_count=1,
            max_timeout_s=1.0,
        )
        reg.register(m)
        # Any reply with the correct (sender, opcode) wakes it,
        # regardless of the option byte.
        ev = _reply_event(self.SENDER, self.OPC_GET_CONFIG, option=0xAA)
        matched = reg.try_match(ev)
        self.assertIs(matched, m)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
