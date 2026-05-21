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
from racelink.transport import LP


class _FakeTransport:
    """Minimal transport stub: just enough for ``send_get_config`` to
    be call-counted."""

    def __init__(self, ident_mac: str = "TEST-GW"):
        # Stage 3 Part C: every transport carries an ident_mac so the
        # matcher's gateway_id filter has a stable anchor.
        self.ident_mac = ident_mac
        self.get_config_calls: list[tuple[bytes, int]] = []

    def send_get_config(self, recv3, option):
        self.get_config_calls.append((bytes(recv3), int(option) & 0xFF))


class _FakeGatewayService:
    """Implements only the surface ``ConfigService.read_config`` touches."""

    def __init__(self, *, replies=None):
        self.transport = _FakeTransport()
        # Stage 3 Part C: production ``ConfigService.read_config`` reads
        # ``gateway_service.controller.transport_for_device(...)`` to
        # route the unicast. The fake has no controller, so the route
        # falls through to ``self.transport``. Mirror the production
        # contract by exposing ``controller=None`` explicitly.
        self.controller = None
        self._replies = list(replies or [])
        self.send_calls: list[dict] = []

    def send_and_match(self, send_fn, matcher, *, transport=None):
        """Phase-2 path: record the matcher's filters for assertions, run
        send_fn, and replay queued replies through ``matcher.matches`` so the
        discriminator filter and structured fields are exercised end-to-end.

        Stage 3 Part C: accepts the new ``transport`` kwarg the
        production ``GatewayService`` exposes for multi-network
        routing. The fake doesn't actually route — it always uses
        ``self.transport`` — but mirroring the signature keeps the
        production-vs-fake contract honest.
        """
        # Surface the filters the caller built so existing tests can keep
        # asserting "discriminator was forwarded as 0x05" etc.
        self.send_calls.append({
            "sender_filter": matcher.sender_filter,
            "expected_opcode": matcher.expected_opcode,
            "expected_ack_of": matcher.expected_ack_of,
            "discriminator": matcher.discriminator_value,
            "max_timeout_s": matcher.max_timeout_s,
            "gateway_id": matcher.gateway_id,
        })
        send_fn()
        # Stage 3 Part C: the matcher carries a concrete gateway_id
        # (from the routed transport); tag each replayed reply so
        # ``matcher.matches`` accepts it. Mirrors the real transport's
        # ``_emit`` tagging.
        for ev in self._replies:
            tagged = dict(ev)
            tagged.setdefault("gateway_id", self.transport.ident_mac)
            if matcher.matches(tagged):
                matcher.collected.append(tagged)
                if len(matcher.collected) >= matcher.expected_count:
                    break
        return list(matcher.collected), "count" if matcher.collected else "no_reply"


def _make_dev(addr: str = "AABBCCDDEEFF"):
    return RL_Device(addr, RL_Dev_Type.NODE_WLED_REV5, "WLED", caps=RL_Dev_Type.NODE_WLED_REV5)


class ReadConfigTests(unittest.TestCase):
    def test_returns_parsed_tuple_on_success(self):
        replies = [{
            "opc": int(LP.OPC_GET_CONFIG),
            "sender3": bytes.fromhex("DDEEFF"),
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
