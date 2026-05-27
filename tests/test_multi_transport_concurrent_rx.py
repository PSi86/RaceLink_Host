"""Multi-transport RX concurrency tests (2026-05-26 follow-up to Bug 5 fix).

Pins the contract that concurrent inbound replies from independent gateway
transports are routed correctly and do not cross-talk:

  1. Two PendingMatchers tagged with distinct ``gateway_id`` values each
     collect ONLY their own gateway's replies, even when the events are
     fed in by two concurrent threads simulating parallel RX-reader feeds.
  2. Two concurrent IDENTIFY_REPLY events for distinct device MACs from
     two transports both update the device repository and signal their
     per-mac wait events without races / lost updates.

Companion to the lock-ordering invariant comment in
``GatewayService.on_transport_event`` (IDENTIFY_REPLY branch): the comment
says concurrent identify replies from different gateways are safe; this
test exercises that path.
"""

from __future__ import annotations

import threading
import unittest

from racelink.services.pending_requests import (
    PendingMatcher,
    PendingMatcherRegistry,
)


class ConcurrentMatcherRoutingTests(unittest.TestCase):
    """Two matchers, two gateway_ids, two threads — no cross-talk."""

    def _status_event(self, *, sender3, gateway_id):
        return {
            "opc": 0x03,  # OPC_STATUS
            "reply": "STATUS_REPLY",
            "sender3": sender3,
            "gateway_id": gateway_id,
            "flags": 0,
            "configByte": 0,
            "effectId": 0,
            "brightness": 0,
            "vbat_mV": 0,
            "node_rssi": -60,
            "node_snr": 7,
            "host_rssi": -60,
            "host_snr": 7,
        }

    def test_concurrent_status_replies_route_to_separate_matchers(self):
        # Two matchers, each scoped to its own gateway_id. ``sender_filter``
        # is None (wildcard) because broadcast OPC_STATUS replies can come
        # from any device on the bound gateway. The registry routes by
        # gateway_id via ``matches`` instead.
        registry = PendingMatcherRegistry()
        matcher_a = PendingMatcher(
            sender_filter=None,
            expected_opcode=0x03,
            gateway_id="GW-A",
            discriminator_field="reply",
            discriminator_value="STATUS_REPLY",
            expected_count=1,
            idle_timeout_s=0.1,
            max_timeout_s=2.0,
        )
        matcher_b = PendingMatcher(
            sender_filter=None,
            expected_opcode=0x03,
            gateway_id="GW-B",
            discriminator_field="reply",
            discriminator_value="STATUS_REPLY",
            expected_count=1,
            idle_timeout_s=0.1,
            max_timeout_s=2.0,
        )
        registry.register(matcher_a)
        registry.register(matcher_b)

        # Barrier so both threads emit at the same instant. Without this
        # the test would serialise on Python's GIL despite the threads;
        # the barrier forces the matcher.try_match call sites to race for
        # the registry lock.
        barrier = threading.Barrier(2)

        def emit_a():
            barrier.wait()
            for _ in range(20):  # repeat to widen the race window
                registry.try_match(self._status_event(
                    sender3=b"\xAA\xAA\xAA", gateway_id="GW-A",
                ))

        def emit_b():
            barrier.wait()
            for _ in range(20):
                registry.try_match(self._status_event(
                    sender3=b"\xBB\xBB\xBB", gateway_id="GW-B",
                ))

        ta = threading.Thread(target=emit_a, name="emit-a")
        tb = threading.Thread(target=emit_b, name="emit-b")
        ta.start()
        tb.start()
        ta.join(timeout=2.0)
        tb.join(timeout=2.0)
        self.assertFalse(ta.is_alive())
        self.assertFalse(tb.is_alive())

        # Each matcher must have collected ONLY its own gateway's replies.
        for ev in matcher_a.collected:
            self.assertEqual(
                ev.get("gateway_id"), "GW-A",
                f"matcher_a leaked a cross-gateway reply: {ev}",
            )
        for ev in matcher_b.collected:
            self.assertEqual(
                ev.get("gateway_id"), "GW-B",
                f"matcher_b leaked a cross-gateway reply: {ev}",
            )
        # Both must have collected at least one (matchers stop at
        # expected_count=1, so additional emits past the first hit go
        # nowhere — that's fine, the contract here is just "no cross-
        # talk", not "every emit landed").
        self.assertGreaterEqual(len(matcher_a.collected), 1)
        self.assertGreaterEqual(len(matcher_b.collected), 1)


class ConcurrentIdentifyReplyTests(unittest.TestCase):
    """Two IDENTIFY_REPLYs on two transports for two distinct MACs
    must both land in the device repository without races. Exercises
    the lock-ordering invariant documented in
    ``GatewayService.on_transport_event``.
    """

    def _make_controller_and_service(self):
        # Reuse the FakeController/FakeTransport from test_gateway_service —
        # local import to avoid a hard cross-module dependency.
        from tests.test_gateway_service import FakeController, FakeTransport
        from racelink.services.gateway_service import GatewayService

        controller = FakeController()
        # Attach a second transport so the IDENTIFY-REPLY paths can route
        # by ev.gateway_id. Both share the same controller's repository,
        # which is exactly the prod shape.
        controller._transports.append(FakeTransport())
        controller._transports[0].ident_mac = "GW-A"
        controller._transports[1].ident_mac = "GW-B"
        service = GatewayService(controller)
        return controller, service

    def test_concurrent_identify_replies_update_distinct_devices(self):
        from racelink.transport import LP

        controller, service = self._make_controller_and_service()

        # Two fresh MACs not yet in the repo. The handler appends new
        # devices under _state_lock; the test pins both append+set
        # operations and the per-mac Event signal.
        mac_a = "112233445566"
        mac_b = "778899AABBCC"

        barrier = threading.Barrier(2)

        def emit(mac_hex: str, gw_id: str):
            barrier.wait()
            service.on_transport_event({
                "opc": LP.OPC_DEVICES,
                "reply": "IDENTIFY_REPLY",
                "mac6": bytes.fromhex(mac_hex),
                "groupId": 0,
                "caps": 1,
                "version": 7,
                "host_rssi": -50,
                "host_snr": 8,
                "gateway_id": gw_id,
            })
            # ``_restore_known_device_group`` spawns a worker thread for
            # known devices with stored groupId; new devices (groupId=0
            # both in repo and on the wire) skip that. Wait briefly just
            # in case to keep teardown clean.
            service._join_auto_restore_workers(timeout=2.0)

        ta = threading.Thread(target=emit, args=(mac_a, "GW-A"), name="emit-a")
        tb = threading.Thread(target=emit, args=(mac_b, "GW-B"), name="emit-b")
        ta.start()
        tb.start()
        ta.join(timeout=3.0)
        tb.join(timeout=3.0)
        self.assertFalse(ta.is_alive())
        self.assertFalse(tb.is_alive())

        # Both devices must be in the repository. The FakeController's
        # ``getDeviceFromAddress`` only knows about its pre-seeded
        # ``self.dev``, so scan ``device_repository.list()`` directly to
        # verify the appends from on_transport_event landed.
        macs_in_repo = {
            (getattr(d, "addr", "") or "").upper()
            for d in controller.device_repository.list()
        }
        self.assertIn(mac_a, macs_in_repo,
                      "device A missing from repository after concurrent IDENTIFY")
        self.assertIn(mac_b, macs_in_repo,
                      "device B missing from repository after concurrent IDENTIFY")
        # And the per-mac identify events must be set, so wait_for_identify
        # callers (OTA, etc.) wake up correctly.
        self.assertTrue(service.wait_for_identify(mac_a, timeout_s=0.1))
        self.assertTrue(service.wait_for_identify(mac_b, timeout_s=0.1))


class StatusServiceFansOutViaBroadcastFanoutTests(unittest.TestCase):
    """Pins that get_status(broadcast) goes through broadcast_fanout
    rather than the older sequential per-transport for-loop. The
    timing benefit (wall-clock = 1× airtime, not N×) is a consequence
    of using broadcast_fanout; testing the call shape itself is
    deterministic and unit-test-stable.
    """

    def test_get_status_broadcast_uses_broadcast_fanout(self):
        from unittest.mock import patch

        from racelink.domain import RL_Device
        from racelink.services.status_service import StatusService
        from racelink.transport import LP
        from tests.test_services_discovery_status import (
            FakeController,
            FakeGateway,
            FakeTransport,
        )

        gw_a = FakeTransport(ident_mac="GW-A")
        gw_b = FakeTransport(ident_mac="GW-B")
        dev_a = RL_Device("AABBCCDDEEFF", 1, "Node A", groupId=255)
        dev_a.network_id = "GW-A"
        dev_b = RL_Device("001122334455", 1, "Node B", groupId=255)
        dev_b.network_id = "GW-B"
        controller = FakeController([dev_a, dev_b], transports=[gw_a, gw_b])
        gateway = FakeGateway([
            {
                "opc": LP.OPC_STATUS,
                "reply": "STATUS_REPLY",
                "mac6": bytes.fromhex("AABBCCDDEEFF"),
                "sender3": bytes.fromhex("DDEEFF"),
                "gateway_id": "GW-A",
            },
            {
                "opc": LP.OPC_STATUS,
                "reply": "STATUS_REPLY",
                "mac6": bytes.fromhex("001122334455"),
                "sender3": bytes.fromhex("334455"),
                "gateway_id": "GW-B",
            },
        ], got_closed=True)
        service = StatusService(controller, gateway)

        with patch(
            "racelink.services.status_service.broadcast_fanout",
            wraps=__import__(
                "racelink.transport.broadcast_fanout", fromlist=["broadcast_fanout"]
            ).broadcast_fanout,
        ) as spy:
            result = service.get_status(group_filter=255)

        # broadcast_fanout must have been invoked exactly once — that's
        # the contract change from sequential to threaded fan-out.
        self.assertEqual(spy.call_count, 1,
                         "get_status broadcast must dispatch via broadcast_fanout")
        # Both transports got a send and both responders are aggregated
        # — same outcome as the pre-refactor sequential path, just via
        # the fan-out helper.
        self.assertEqual(
            [s[0] for s in gw_a.sent], ["status"],
            "GW-A must have received an OPC_STATUS broadcast",
        )
        self.assertEqual(
            [s[0] for s in gw_b.sent], ["status"],
            "GW-B must have received an OPC_STATUS broadcast",
        )
        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["responders"], {"AABBCCDDEEFF", "001122334455"})


if __name__ == "__main__":
    unittest.main()
