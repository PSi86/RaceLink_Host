"""End-to-end test for ConfigService.read_config.

A fake gateway-service emits the expected ``GET_CONFIG_REPLY`` event
synchronously inside the caller's ``send_fn`` so the
``send_and_wait_for_reply`` waiter unblocks immediately. The test
asserts the (option, data0..3) tuple is parsed from the reply event
and that broadcast / address-resolution failure paths return ``None``.
"""

from __future__ import annotations

import unittest

from racelink.domain import RL_Device, RL_Dev_Type
from racelink.services.config_service import ConfigService


class _FakeTransport:
    """Minimal transport stub: just enough for ``send_get_config`` to
    be call-counted."""

    def __init__(self):
        self.get_config_calls: list[tuple[bytes, int]] = []

    def send_get_config(self, recv3, option):
        self.get_config_calls.append((bytes(recv3), int(option) & 0xFF))


class _FakeGatewayService:
    """Implements only the surface ``ConfigService.read_config`` touches."""

    def __init__(self, *, replies=None):
        self.transport = _FakeTransport()
        self._replies = list(replies or [])
        self.send_calls: list[dict] = []

    def send_and_wait_for_reply(self, recv3, opcode7, send_fn, *, timeout_s=None, discriminator=None):
        self.send_calls.append({
            "recv3": bytes(recv3),
            "opcode7": int(opcode7),
            "timeout_s": timeout_s,
            "discriminator": discriminator,
        })
        send_fn()
        return list(self._replies), bool(self._replies)


def _make_dev(addr: str = "AABBCCDDEEFF"):
    return RL_Device(addr, RL_Dev_Type.NODE_WLED_REV5, "WLED", caps=RL_Dev_Type.NODE_WLED_REV5)


class ReadConfigTests(unittest.TestCase):
    def test_returns_parsed_tuple_on_success(self):
        replies = [{
            "reply": "GET_CONFIG_REPLY",
            "option": 0x05,
            "data0": 60,
            "data1": 0,
            "data2": 0,
            "data3": 0,
        }]
        gw = _FakeGatewayService(replies=replies)
        svc = ConfigService(controller=None, gateway_service=gw)

        result = svc.read_config(_make_dev(), option=0x05, timeout_s=0.5)
        self.assertEqual(result, (0x05, 60, 0, 0, 0))

        # Wire-side: send_get_config invoked once with recv3 = last 3 MAC bytes.
        self.assertEqual(gw.transport.get_config_calls, [(bytes.fromhex("DDEEFF"), 0x05)])

        # Iteration-3 fix: the option byte is forwarded as the registry's
        # secondary discriminator so concurrent reads for different
        # options can't wake each other's waiter.
        self.assertEqual(len(gw.send_calls), 1)
        self.assertEqual(gw.send_calls[0]["discriminator"], 0x05)

    def test_returns_none_on_no_reply(self):
        gw = _FakeGatewayService(replies=[])
        svc = ConfigService(controller=None, gateway_service=gw)
        self.assertIsNone(svc.read_config(_make_dev(), option=0x05, timeout_s=0.1))

    def test_refuses_broadcast_address(self):
        gw = _FakeGatewayService(replies=[])
        svc = ConfigService(controller=None, gateway_service=gw)
        # An all-FF MAC resolves to recv3 = FFFFFF — refused upfront.
        dev = _make_dev("FFFFFFFFFFFF")
        self.assertIsNone(svc.read_config(dev, option=0x05))
        # No wire send attempted.
        self.assertEqual(gw.transport.get_config_calls, [])

    def test_refuses_unparseable_address(self):
        gw = _FakeGatewayService(replies=[])
        svc = ConfigService(controller=None, gateway_service=gw)
        dev = _make_dev("")  # blank addr
        self.assertIsNone(svc.read_config(dev, option=0x05))
        self.assertEqual(gw.transport.get_config_calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
