import unittest

from racelink.domain import RL_Device
from racelink.services.discovery_service import DiscoveryService
from racelink.services.status_service import StatusService
from racelink.transport import LP


class FakeTransport:
    def __init__(self, ident_mac="TEST-GW"):
        # Stage 3 Part C: every transport carries an ident_mac so the
        # matcher's gateway_id filter has an anchor.
        self.ident_mac = ident_mac
        self.sent = []

    def send_get_devices(self, **kwargs):
        self.sent.append(("devices", kwargs))

    def send_get_status(self, **kwargs):
        self.sent.append(("status", kwargs))

    def drain_events(self, timeout_s=0.0):
        return []


class FakeGateway:
    def __init__(self, events, got_closed=True):
        self.events = events
        self.got_closed = got_closed
        self.installed = False

    def install_transport_hooks(self, transport=None):
        # Stage 3 Part F: signature now matches the production
        # ``GatewayService.install_transport_hooks`` which accepts an
        # explicit transport for multi-network call sites. Tests
        # don't care which transport — the singleton fake stays in
        # play either way.
        self.installed = True
        self.last_install_transport = transport

    def wait_rx_window(self, send_fn, collect_pred=None, fail_safe_s=8.0, *, stop_on_match=False):
        send_fn()
        collected = []
        for ev in self.events:
            if collect_pred and collect_pred(ev):
                collected.append(ev)
                if stop_on_match:
                    break
        return collected, self.got_closed

    def send_and_match(self, send_fn, matcher, *, transport=None):
        """Test shim mirroring the prod send_and_match: replay events
        through matcher.matches(), append to matcher.collected, exit early
        on expected_count.

        Stage 3 Part C: mirrors the production signature's new
        ``transport`` kwarg and tags every replayed event with the
        matcher's gateway_id (if any) so concrete-sender matchers
        accept the replayed reply — the real transport's ``_emit``
        applies the same tag.
        """
        send_fn()
        # Determine the gateway_id to stamp on events. Prefer the
        # matcher's filter (if any), otherwise fall back to the
        # routed transport's ident_mac, otherwise leave events
        # untagged (legitimate for wildcard matchers).
        ev_gateway_id = matcher.gateway_id or (
            getattr(transport, "ident_mac", None) if transport else None
        )
        for ev in self.events:
            tagged = dict(ev)
            if ev_gateway_id is not None:
                tagged.setdefault("gateway_id", ev_gateway_id)
            if matcher.matches(tagged):
                matcher.collected.append(tagged)
                if len(matcher.collected) >= matcher.expected_count:
                    break
        return list(matcher.collected), "count" if matcher.collected else "no_reply"

    @staticmethod
    def compute_collect_max_timeout(expected, *, base_s=1.0, per_device_s=0.15, ceiling_s=5.0):
        n = max(0, int(expected))
        return min(ceiling_s, base_s + n * float(per_device_s))


class FakeController:
    def __init__(self, devices, transports=None):
        self._devices = devices
        self.transport = FakeTransport()
        # Multi-transport fan-out (Bug 5 fix, 2026-05-26): tests that
        # exercise the per-transport branch supply a list; default keeps
        # the legacy single-transport behaviour where every device
        # routes to ``self.transport``.
        self._transports = list(transports) if transports is not None else None
        self.group_assignments = []

    def _to_hex_str(self, value):
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).hex().upper()
        return str(value or "").upper()

    def getDeviceFromAddress(self, addr):
        want = str(addr or "").upper()
        for dev in self._devices:
            if dev.addr == want or dev.addr.endswith(want):
                return dev
        return None

    def setNodeGroupId(self, dev):
        self.group_assignments.append((dev.addr, dev.groupId))

    def reconcile_group_network(self, group_id):
        # No-op stand-in: the discovery add-to-group path calls this to
        # stamp the joined group's network. The discovery tests assert on
        # group assignment, not on the reconcile side effect.
        self.reconciled_groups = getattr(self, "reconciled_groups", [])
        self.reconciled_groups.append(int(group_id))
        return False

    @property
    def device_repository(self):
        class Repo:
            def __init__(self, items):
                self._items = items

            def list(self):
                return self._items

        return Repo(self._devices)

    @property
    def transports(self):
        # When ``transports=`` wasn't passed, expose the singleton as a
        # 1-element list so resolve_broadcast_transports's "all attached"
        # fan-out keeps producing the same single radio.
        if self._transports is not None:
            return list(self._transports)
        return [self.transport]

    def transport_for_network(self, network_id):
        if not network_id:
            return None
        for t in self.transports:
            ident = str(getattr(t, "ident_mac", "") or "").upper()
            if ident and ident == str(network_id).upper():
                return t
        return None

    def transport_for_device(self, addr):
        # In the legacy single-transport case (no per-device network
        # binding), every device routes to the singleton transport so
        # the status service's snapshot picks them up correctly.
        dev = self.getDeviceFromAddress(addr) if addr else None
        net_id = getattr(dev, "network_id", None) if dev is not None else None
        if not net_id:
            return self.transport
        routed = self.transport_for_network(net_id)
        return routed if routed is not None else self.transport


class DiscoveryAndStatusTests(unittest.TestCase):
    def test_discovery_service_assigns_group_to_responders(self):
        dev = RL_Device("AABBCCDDEEFF", 1, "Node", groupId=0)
        controller = FakeController([dev])
        gateway = FakeGateway(
            [
                {
                    "opc": LP.OPC_DEVICES,
                    "reply": "IDENTIFY_REPLY",
                    "mac6": bytes.fromhex("AABBCCDDEEFF"),
                    "sender3": bytes.fromhex("DDEEFF"),
                }
            ]
        )
        service = DiscoveryService(controller, gateway)

        result = service.discover_devices(group_filter=0, add_to_group=4)

        self.assertTrue(gateway.installed)
        self.assertEqual(result["found"], 1)
        self.assertEqual(result["responders"], {"AABBCCDDEEFF"})
        self.assertEqual(dev.groupId, 4)
        self.assertEqual(controller.group_assignments, [("AABBCCDDEEFF", 4)])

    def test_discovery_service_in_groups_sweeps_each_id(self):
        """``discover_devices_in_groups`` fans out one OPC_DEVICES per
        group id and merges responders. Used by the WebUI's "Discover
        in: All groups" sweep — see broadcast-ruleset.md and the
        roadmap entry for the future single-packet replacement.
        """
        dev_a = RL_Device("AABBCCDDEEFF", 1, "A", groupId=2)
        dev_b = RL_Device("001122334455", 1, "B", groupId=3)
        controller = FakeController([dev_a, dev_b])
        # FakeGateway returns the same canned events for every send;
        # both group-2 and group-3 sweeps will record the same
        # IDENTIFY_REPLY twice. The assertion is on the SEND fan-out
        # count, not on responder uniqueness post-sweep.
        gateway = FakeGateway(
            [
                {
                    "opc": LP.OPC_DEVICES,
                    "reply": "IDENTIFY_REPLY",
                    "mac6": bytes.fromhex("AABBCCDDEEFF"),
                    "sender3": bytes.fromhex("DDEEFF"),
                }
            ]
        )
        service = DiscoveryService(controller, gateway)

        result = service.discover_devices_in_groups(group_ids=[2, 3])

        # Two sends, one per group filter.
        send_calls = [s for s in controller.transport.sent if s[0] == "devices"]
        self.assertEqual(len(send_calls), 2)
        emitted_filters = sorted(call[1].get("group_id") for call in send_calls)
        self.assertEqual(emitted_filters, [2, 3])
        # Responders merge into a set (no duplicates even though the
        # canned reply fired twice).
        self.assertEqual(result["responders"], {"AABBCCDDEEFF"})

    def test_discovery_service_in_groups_skips_invalid_ids(self):
        # Out-of-range / non-int ids are skipped silently rather than
        # crashing the sweep — the API may pass through malformed input
        # and the worker shouldn't blow up the task.
        controller = FakeController([])
        gateway = FakeGateway([])
        service = DiscoveryService(controller, gateway)

        result = service.discover_devices_in_groups(
            group_ids=[1, 255, -1, "bogus", 5],
        )

        send_calls = [s for s in controller.transport.sent if s[0] == "devices"]
        emitted = sorted(call[1].get("group_id") for call in send_calls)
        self.assertEqual(emitted, [1, 5])
        self.assertEqual(result["found"], 0)

    def test_discovery_service_broadcasts_on_every_attached_transport(self):
        """Operator "Discover Devices" must send OPC_DEVICES on EVERY
        attached transport, not just the primary slot — so a second RF
        gateway and an Ethernet network are both probed regardless of
        attach order. Mirrors the status-service fan-out (Bug 5)."""
        gw_a = FakeTransport(ident_mac="GW-A")
        gw_eth = FakeTransport(ident_mac="ETH:net-eth")
        dev_a = RL_Device("AABBCCDDEEFF", 1, "A", groupId=0, network_id="GW-A")
        dev_eth = RL_Device("001122334455", 1, "ETH", groupId=0, network_id="net-eth")
        controller = FakeController([dev_a, dev_eth], transports=[gw_a, gw_eth])
        gateway = FakeGateway(
            [
                {
                    "opc": LP.OPC_DEVICES,
                    "reply": "IDENTIFY_REPLY",
                    "mac6": bytes.fromhex("AABBCCDDEEFF"),
                    "sender3": bytes.fromhex("DDEEFF"),
                },
                {
                    "opc": LP.OPC_DEVICES,
                    "reply": "IDENTIFY_REPLY",
                    "mac6": bytes.fromhex("001122334455"),
                    "sender3": bytes.fromhex("334455"),
                },
            ]
        )
        service = DiscoveryService(controller, gateway)

        result = service.discover_devices(group_filter=0)

        # One OPC_DEVICES per attached transport — neither is skipped.
        a_sends = [s for s in gw_a.sent if s[0] == "devices"]
        eth_sends = [s for s in gw_eth.sent if s[0] == "devices"]
        self.assertEqual(len(a_sends), 1)
        self.assertEqual(len(eth_sends), 1)
        # Responders from both transports merge into the aggregated result.
        self.assertEqual(result["responders"], {"AABBCCDDEEFF", "001122334455"})

    def test_status_service_marks_non_responders_offline_on_window_close(self):
        responding = RL_Device("AABBCCDDEEFF", 1, "Node A", groupId=2)
        silent = RL_Device("001122334455", 1, "Node B", groupId=2)
        controller = FakeController([responding, silent])
        gateway = FakeGateway(
            [
                {
                    "opc": LP.OPC_STATUS,
                    "reply": "STATUS_REPLY",
                    "mac6": bytes.fromhex("AABBCCDDEEFF"),
                    "sender3": bytes.fromhex("DDEEFF"),
                }
            ],
            got_closed=True,
        )
        service = StatusService(controller, gateway)

        result = service.get_status(group_filter=2)

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["responders"], {"AABBCCDDEEFF"})
        self.assertFalse(silent.link_online)
        self.assertEqual(silent.link_error, "Missing reply (STATUS)")

    def test_status_service_broadcasts_on_every_attached_transport(self):
        """Bug 5 fix (2026-05-26): a multi-gateway setup must broadcast
        OPC_STATUS on every attached transport, scoped per-transport
        replies, and aggregate the responders. Without this, the off-
        radio device on the secondary gateway falls into the per-device
        retry path on every poll (``retried=1, retried_success=1``)
        instead of answering directly off the broadcast.
        """
        gw_a_transport = FakeTransport(ident_mac="GW-A")
        gw_b_transport = FakeTransport(ident_mac="GW-B")
        dev_a = RL_Device("AABBCCDDEEFF", 1, "Node A", groupId=255)
        dev_a.network_id = "GW-A"
        dev_b = RL_Device("001122334455", 1, "Node B", groupId=255)
        dev_b.network_id = "GW-B"
        controller = FakeController(
            [dev_a, dev_b],
            transports=[gw_a_transport, gw_b_transport],
        )
        # Each transport reports its own device. The FakeGateway tags
        # replayed events with the matcher's gateway_id, so the
        # per-transport matcher will only accept the matching event.
        gateway = FakeGateway(
            [
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
            ],
            got_closed=True,
        )
        service = StatusService(controller, gateway)

        result = service.get_status(group_filter=255)

        # Both transports got a broadcast send.
        self.assertEqual(
            [s[0] for s in gw_a_transport.sent], ["status"],
            "GW-A must have received an OPC_STATUS broadcast",
        )
        self.assertEqual(
            [s[0] for s in gw_b_transport.sent], ["status"],
            "GW-B must have received an OPC_STATUS broadcast",
        )
        # Both devices end up in the aggregated responder set; no retry
        # round needed because each device answered its own gateway's
        # broadcast directly.
        self.assertEqual(result["updated"], 2)
        self.assertEqual(
            result["responders"], {"AABBCCDDEEFF", "001122334455"},
        )
        self.assertEqual(result["retried"], 0)
        self.assertEqual(result["retried_responders"], set())


if __name__ == "__main__":
    unittest.main()
