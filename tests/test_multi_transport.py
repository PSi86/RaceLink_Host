"""Multi-transport scaffolding tests (Stage 2 part 2).

Pins three contracts the Stage-3 multi-network code paths will depend on:

  1. ``PendingMatcher.gateway_id`` filters events by transport identity
     (and stays wildcard-compatible when ``gateway_id is None``).
  2. The controller's ``self.transport`` read/write contract is
     preserved while the internal store is now a list.
  3. ``transport_for_network`` / ``transport_for_device`` route to the
     correct transport based on the network's ``gateway_mac`` binding.
"""

from __future__ import annotations

import unittest

from racelink.services.pending_requests import PendingMatcher


class PendingMatcherGatewayIdTests(unittest.TestCase):

    def _ack_event(self, *, opc=0x7E, ack_of=0x0D, ack_status=0,
                   sender3=b"\xDD\xEE\xFF", gateway_id=None):
        ev = {
            "opc": opc,
            "ack_of": ack_of,
            "ack_status": ack_status,
            "sender3": sender3,
        }
        if gateway_id is not None:
            ev["gateway_id"] = gateway_id
        return ev

    def test_wildcard_matcher_ignores_gateway_id(self):
        # gateway_id=None (default) -> the matcher's pure-filter
        # behaviour accepts events from any transport, including
        # untagged events. Stage-3 hardens registration to reject
        # this combination for concrete-sender matchers
        # (see PendingMatcherRegistryGatewayIdEnforcementTests below);
        # the ``matches`` method itself remains a pure predicate.
        m = PendingMatcher(
            sender_filter=frozenset({b"\xDD\xEE\xFF"}),
            expected_ack_of=0x0D,
        )
        self.assertTrue(m.matches(self._ack_event()))  # no gateway_id
        self.assertTrue(m.matches(self._ack_event(gateway_id="GW-A")))
        self.assertTrue(m.matches(self._ack_event(gateway_id="GW-B")))

    def test_specific_gateway_id_filters_other_transports(self):
        m = PendingMatcher(
            sender_filter=frozenset({b"\xDD\xEE\xFF"}),
            expected_ack_of=0x0D,
            gateway_id="GW-A",
        )
        self.assertTrue(m.matches(self._ack_event(gateway_id="GW-A")))
        self.assertFalse(m.matches(self._ack_event(gateway_id="GW-B")))
        # Untagged events (legacy single-gateway transport) do NOT
        # match a concrete gateway_id filter — that filter exists
        # exactly to differentiate transports.
        self.assertFalse(m.matches(self._ack_event(gateway_id=None)))


class ControllerTransportListShimTests(unittest.TestCase):
    """The ``self.transport`` read/write contract is preserved by a
    property over ``self._transports``."""

    def _make_controller(self):
        # Lazy import so the test can run without a configured host_api.
        from racelink.controller import RaceLink_Host

        class _FakeHostApi:
            class _Db:
                def option(self, key, default=None):
                    return default

                def set_option(self, key, value):
                    pass

            def __init__(self):
                self.db = _FakeHostApi._Db()

            def fire_event(self, *_a, **_kw):
                pass

            def log(self, *_a, **_kw):
                pass

        host = RaceLink_Host(_FakeHostApi(), "test", "Test")
        return host

    def test_default_transport_is_none(self):
        host = self._make_controller()
        self.assertIsNone(host.transport)
        self.assertEqual(host.transports, [])

    def test_setter_with_value_populates_first_slot(self):
        host = self._make_controller()
        sentinel = object()
        host.transport = sentinel
        self.assertIs(host.transport, sentinel)
        self.assertEqual(len(host.transports), 1)
        self.assertIs(host.transports[0], sentinel)

    def test_setter_with_none_clears_list(self):
        host = self._make_controller()
        host.transport = object()
        host.transport = None
        self.assertIsNone(host.transport)
        self.assertEqual(host.transports, [])


class TransportForNetworkRoutingTests(unittest.TestCase):

    def _make_controller(self):
        from racelink.controller import RaceLink_Host

        class _FakeHostApi:
            class _Db:
                def option(self, key, default=None):
                    return default

                def set_option(self, key, value):
                    pass

            def __init__(self):
                self.db = _FakeHostApi._Db()

            def fire_event(self, *_a, **_kw):
                pass

            def log(self, *_a, **_kw):
                pass

        return RaceLink_Host(_FakeHostApi(), "test", "Test")

    def test_transport_for_network_falls_back_to_single_transport(self):
        # No gateway_mac binding on the network -> Stage 2 fallback
        # returns the only transport, mirroring legacy behaviour.
        host = self._make_controller()
        # The v1->v2 migration has already populated the default network
        # (load_from_db isn't called here but the network repository
        # would otherwise be empty; we drive the fallback path
        # explicitly by setting only one transport).
        sentinel = object()
        host.transport = sentinel
        # No networks registered: still falls back to single transport.
        self.assertIs(host.transport_for_network("any-id"), sentinel)

    def test_transport_for_network_resolves_via_gateway_mac(self):
        from racelink.domain.models import RL_Network

        host = self._make_controller()

        class _FakeTransport:
            def __init__(self, mac):
                self.ident_mac = mac

        t_a = _FakeTransport("AA:BB:CC:DD:EE:01")
        t_b = _FakeTransport("AA:BB:CC:DD:EE:02")
        host._transports = [t_a, t_b]

        net = RL_Network(name="Track A", gateway_mac="AA:BB:CC:DD:EE:02")
        host.network_repository.append(net)
        self.assertIs(host.transport_for_network(net.id), t_b)

    def test_transport_for_network_unknown_id_returns_none(self):
        host = self._make_controller()
        # No transports, no networks.
        self.assertIsNone(host.transport_for_network("nonexistent"))

    def test_transport_for_network_with_multiple_transports_and_no_mac_returns_none(self):
        from racelink.domain.models import RL_Network

        host = self._make_controller()

        class _FakeTransport:
            def __init__(self, mac):
                self.ident_mac = mac

        host._transports = [_FakeTransport("AA"), _FakeTransport("BB")]
        # Network without gateway_mac binding + multiple transports
        # → ambiguous, returns None (the single-transport fallback in
        # transport_for_network only fires when len==1).
        net = RL_Network(name="Unbound", gateway_mac=None)
        host.network_repository.append(net)
        self.assertIsNone(host.transport_for_network(net.id))


class PendingMatcherRegistryGatewayIdEnforcementTests(unittest.TestCase):
    """Stage 3 Part C contract: the registry refuses to admit a
    concrete-sender matcher without a ``gateway_id``.

    Wildcard matchers (``sender_filter is None`` — discovery /
    fleet-wide broadcasts) may still omit ``gateway_id``: they
    collect from every transport by design.
    """

    def test_register_rejects_unicast_without_gateway_id(self):
        from racelink.services.pending_requests import PendingMatcherRegistry

        reg = PendingMatcherRegistry()
        m = PendingMatcher(
            sender_filter=frozenset({b"\xDD\xEE\xFF"}),
            expected_ack_of=0x0D,
            gateway_id=None,
        )
        with self.assertRaises(ValueError) as cm:
            reg.register(m)
        # Error message points the developer at the right helper.
        self.assertIn("gateway_id", str(cm.exception))
        self.assertIn("send_and_wait_with_retries", str(cm.exception))

    def test_register_rejects_multi_sender_without_gateway_id(self):
        # Multi-sender N-reply matcher (e.g. stream-ACK collector)
        # also requires a concrete gateway_id — the target group lives
        # on one network and the matcher must be scoped to that
        # transport.
        from racelink.services.pending_requests import PendingMatcherRegistry

        reg = PendingMatcherRegistry()
        m = PendingMatcher(
            sender_filter=frozenset({b"\xAA\xAA\xAA", b"\xBB\xBB\xBB"}),
            expected_ack_of=0x05,
            gateway_id=None,
        )
        with self.assertRaises(ValueError):
            reg.register(m)

    def test_register_accepts_unicast_with_gateway_id(self):
        from racelink.services.pending_requests import PendingMatcherRegistry

        reg = PendingMatcherRegistry()
        m = PendingMatcher(
            sender_filter=frozenset({b"\xDD\xEE\xFF"}),
            expected_ack_of=0x0D,
            gateway_id="GW-A",
        )
        # No raise — the registry accepts this and routes by gateway_id.
        reg.register(m)
        # Cleanup so the matcher's thread / wait state doesn't leak.
        reg.cancel(m)

    def test_register_accepts_wildcard_matcher_without_gateway_id(self):
        # Wildcard sender_filter (None) is the broadcast/discovery
        # shape — the Stage-3 contract still allows omitting the
        # gateway_id because the matcher genuinely wants to collect
        # from every attached transport.
        from racelink.services.pending_requests import PendingMatcherRegistry

        reg = PendingMatcherRegistry()
        m = PendingMatcher(
            sender_filter=None,
            expected_opcode=0x0A,
            gateway_id=None,
            expected_count=2**31,
            idle_timeout_s=0.5,
            max_timeout_s=1.0,
        )
        reg.register(m)
        reg.cancel(m)


class PerGatewayPendingExpectTests(unittest.TestCase):
    """Stage 2 Part 3: controller._pending_expect is now keyed by
    gateway_id so two simultaneously-active transports cannot
    overwrite each other's in-flight expectations."""

    def _make_controller(self):
        from racelink.controller import RaceLink_Host

        class _FakeHostApi:
            class _Db:
                def option(self, key, default=None):
                    return default

                def set_option(self, key, value):
                    pass

            def __init__(self):
                self.db = _FakeHostApi._Db()

            def fire_event(self, *_a, **_kw):
                pass

            def log(self, *_a, **_kw):
                pass

        return RaceLink_Host(_FakeHostApi(), "test", "Test")

    def test_set_pending_expect_keyed_by_gateway_id(self):
        host = self._make_controller()
        host.set_pending_expect(
            dev=object(), rule=object(), opcode7=0x0D,
            sender_last3="AABBCC", ts=1.0, gateway_id="GW-A",
        )
        host.set_pending_expect(
            dev=object(), rule=object(), opcode7=0x0E,
            sender_last3="DDEEFF", ts=2.0, gateway_id="GW-B",
        )

        a = host.read_pending_expect(gateway_id="GW-A")
        b = host.read_pending_expect(gateway_id="GW-B")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(a["opcode7"], 0x0D)  # type: ignore[index]
        self.assertEqual(b["opcode7"], 0x0E)  # type: ignore[index]
        # CAS-clear on A leaves B alone.
        self.assertTrue(host.clear_pending_expect_if(a))
        self.assertIsNone(host.read_pending_expect(gateway_id="GW-A"))
        self.assertIsNotNone(host.read_pending_expect(gateway_id="GW-B"))

    def test_read_pending_expect_legacy_fallback(self):
        """With only one entry stamped, ``read_pending_expect(None)``
        still returns it — preserves the Stage-2 wildcard read for
        any caller that has not yet been threaded through with a
        concrete gateway_id."""
        host = self._make_controller()
        host.set_pending_expect(
            dev=object(), rule=object(), opcode7=0x0D,
            sender_last3="AABBCC", ts=1.0, gateway_id="GW-A",
        )
        legacy = host.read_pending_expect()  # no kwarg
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy["opcode7"], 0x0D)  # type: ignore[index]

    def test_clear_pending_expect_if_only_clears_owner(self):
        host = self._make_controller()
        host.set_pending_expect(
            dev=object(), rule=object(), opcode7=0x0D,
            sender_last3="AABBCC", ts=1.0, gateway_id="GW-A",
        )
        a = host.read_pending_expect(gateway_id="GW-A")
        # Build a foreign dict that *looks* identical but isn't ``a``.
        foreign = dict(a)  # type: ignore[arg-type]
        self.assertFalse(host.clear_pending_expect_if(foreign))
        # Original is still present.
        self.assertIs(host.read_pending_expect(gateway_id="GW-A"), a)


class PerGatewayRegistryTests(unittest.TestCase):
    """Stage 2 Part 3: PendingMatcherRegistry is split per
    transport (keyed by gateway_id). Matchers tagged with concrete
    ids live in isolated buckets so a sibling-transport's RX feed
    never satisfies them."""

    def _make_gateway_service(self):
        from racelink.controller import RaceLink_Host
        from racelink.services.gateway_service import GatewayService

        class _FakeHostApi:
            class _Db:
                def option(self, key, default=None):
                    return default

                def set_option(self, key, value):
                    pass

            def __init__(self):
                self.db = _FakeHostApi._Db()

            def fire_event(self, *_a, **_kw):
                pass

            def log(self, *_a, **_kw):
                pass

        host = RaceLink_Host(_FakeHostApi(), "test", "Test")
        gs = GatewayService(host)
        return host, gs

    def test_registry_for_creates_distinct_buckets_per_gateway_id(self):
        _, gs = self._make_gateway_service()
        reg_default = gs._registry_for(None)
        reg_a = gs._registry_for("GW-A")
        reg_b = gs._registry_for("GW-B")
        self.assertIsNot(reg_default, reg_a)
        self.assertIsNot(reg_default, reg_b)
        self.assertIsNot(reg_a, reg_b)
        # Idempotent
        self.assertIs(gs._registry_for("GW-A"), reg_a)

    def test_pending_registry_back_compat_returns_default_bucket(self):
        _, gs = self._make_gateway_service()
        # Legacy code paths use ``gs._pending_registry`` directly.
        self.assertIs(gs._pending_registry, gs._registry_for(None))


class TransportEventTaggingTests(unittest.TestCase):
    """Stage 2 Part 3: transport's ``_emit`` / ``_emit_tx`` tag every
    fanned-out event with ``gateway_id = self.ident_mac`` so
    downstream matchers / pending-registries can route. ident_mac
    starts as ``None`` (pre-handshake) — those events stay untagged
    which is the legacy wildcard path."""

    def _make_transport(self, ident_mac=None):
        # Use the real class but skip ``__init__`` (which opens a serial
        # port). We only need _emit / _emit_tx and the listener lists.
        from racelink.transport.gateway_serial import GatewaySerialTransport

        t = GatewaySerialTransport.__new__(GatewaySerialTransport)
        t.ident_mac = ident_mac
        t._listeners = []
        t._tx_listeners = []
        t._q = []
        t._qmax = 1000
        t.on_event = None
        return t

    def test_emit_tags_event_when_ident_mac_present(self):
        t = self._make_transport(ident_mac="GW-A")
        captured = []
        t._listeners.append(captured.append)
        t._emit({"type": 0xAA, "opc": 0x0D})
        self.assertEqual(captured[0]["gateway_id"], "GW-A")

    def test_emit_skips_tag_when_ident_mac_none(self):
        t = self._make_transport(ident_mac=None)
        captured = []
        t._listeners.append(captured.append)
        t._emit({"type": 0xAA, "opc": 0x0D})
        self.assertNotIn("gateway_id", captured[0])

    def test_emit_does_not_overwrite_pretagged_gateway_id(self):
        t = self._make_transport(ident_mac="GW-A")
        captured = []
        t._listeners.append(captured.append)
        t._emit({"type": 0xAA, "gateway_id": "OVERRIDE"})
        self.assertEqual(captured[0]["gateway_id"], "OVERRIDE")

    def test_emit_tx_tags_event(self):
        t = self._make_transport(ident_mac="GW-B")
        captured = []
        t._tx_listeners.append(captured.append)
        t._emit_tx({"type": "TX_M2N", "opc": 0x0D})
        self.assertEqual(captured[0]["gateway_id"], "GW-B")


class BroadcastFanOutTests(unittest.TestCase):
    """Stage 2 Part 3: ``send_broadcast_and_match`` invokes the
    send-factory once per attached transport and collects replies
    through a single wildcard matcher."""

    def _make_gateway_service(self):
        from racelink.controller import RaceLink_Host
        from racelink.services.gateway_service import GatewayService

        class _FakeHostApi:
            class _Db:
                def option(self, key, default=None):
                    return default

                def set_option(self, key, value):
                    pass

            def __init__(self):
                self.db = _FakeHostApi._Db()

            def fire_event(self, *_a, **_kw):
                pass

            def log(self, *_a, **_kw):
                pass

        host = RaceLink_Host(_FakeHostApi(), "test", "Test")
        gs = GatewayService(host)
        return host, gs

    def test_broadcast_fan_out_calls_factory_per_transport(self):
        host, gs = self._make_gateway_service()

        class _FakeTransport:
            def __init__(self, mac):
                self.ident_mac = mac
                self.sends = 0

            def add_listener(self, cb):
                pass

            def add_tx_listener(self, cb):
                pass

        t_a = _FakeTransport("GW-A")
        t_b = _FakeTransport("GW-B")
        host._transports = [t_a, t_b]

        called_with = []
        def factory(t):
            called_with.append(t.ident_mac)
            t.sends += 1

        from racelink.services.pending_requests import PendingMatcher
        # Tiny deadline — we don't expect any reply, just verify both
        # transports got the factory call before the matcher times out.
        m = PendingMatcher(
            sender_filter=None,
            expected_opcode=0x0D,
            expected_count=2**31,
            idle_timeout_s=0.0,
            max_timeout_s=0.01,
        )
        replies, reason = gs.send_broadcast_and_match(factory, m)
        self.assertEqual(sorted(called_with), ["GW-A", "GW-B"])
        self.assertEqual(t_a.sends, 1)
        self.assertEqual(t_b.sends, 1)
        self.assertEqual(replies, [])
        self.assertEqual(reason, "no_reply")

    def test_broadcast_fan_out_returns_empty_with_no_transports(self):
        _host, gs = self._make_gateway_service()
        from racelink.services.pending_requests import PendingMatcher
        m = PendingMatcher(max_timeout_s=0.01)
        replies, reason = gs.send_broadcast_and_match(lambda _t: None, m)
        self.assertEqual(replies, [])
        self.assertEqual(reason, "no_reply")


class UnicastRoutingTests(unittest.TestCase):
    """Stage 2 Part 3: ``send_and_wait_with_retries`` resolves the
    routed transport via ``transport_for_device(recv3)`` so a unicast
    targeted at a network-A device goes through network-A's gateway,
    never through B."""

    def _make_gateway_service(self):
        from racelink.controller import RaceLink_Host
        from racelink.services.gateway_service import GatewayService

        class _FakeHostApi:
            class _Db:
                def option(self, key, default=None):
                    return default

                def set_option(self, key, value):
                    pass

            def __init__(self):
                self.db = _FakeHostApi._Db()

            def fire_event(self, *_a, **_kw):
                pass

            def log(self, *_a, **_kw):
                pass

        host = RaceLink_Host(_FakeHostApi(), "test", "Test")
        gs = GatewayService(host)
        # ``GatewayService.__init__`` set ``host.gateway_service`` via
        # the controller wiring elsewhere; in this lean setup the
        # service holds the host reference so we don't need it.
        return host, gs

    def test_unicast_routes_via_transport_for_device(self):
        from racelink.domain.models import RL_Network
        from racelink.domain import create_device

        host, gs = self._make_gateway_service()

        class _FakeTransport:
            def __init__(self, mac):
                self.ident_mac = mac
                self.sent_recv3 = []

            def add_listener(self, cb):
                pass

            def add_tx_listener(self, cb):
                pass

            def send_set_group(self, recv3, group_id):
                self.sent_recv3.append(bytes(recv3))

        t_a = _FakeTransport("AA:BB:CC:DD:EE:01")
        t_b = _FakeTransport("AA:BB:CC:DD:EE:02")
        host._transports = [t_a, t_b]

        net_a = RL_Network(name="A", gateway_mac="AA:BB:CC:DD:EE:01")
        net_b = RL_Network(name="B", gateway_mac="AA:BB:CC:DD:EE:02")
        host.network_repository.append(net_a)
        host.network_repository.append(net_b)

        dev_a = create_device(addr="AABBCC112233", dev_type=0, name="dev-a")
        dev_a.network_id = net_a.id
        host.device_repository.append(dev_a)

        # ``transport_for_device`` for dev_a should return t_a.
        self.assertIs(host.transport_for_device("AABBCC112233"), t_a)

        # Drive a unicast SET_GROUP through the gateway_service path;
        # only t_a should see the wire frame. Use a tiny per-attempt
        # timeout so the matcher quits fast without any reply.
        from racelink.transport import LP, mac_last3_from_hex
        recv3 = mac_last3_from_hex(dev_a.addr)
        def _send():
            t_a.send_set_group(recv3, 1)

        events, ok = gs.send_and_wait_with_retries(
            recv3, LP.OPC_SET_GROUP, _send,
            attempts=1, per_attempt_timeout_s=0.01,
        )
        # No reply was produced, the wait should time out without
        # matching — that's expected. The important assertion is the
        # routing: t_a got the call, t_b did not.
        self.assertFalse(ok)
        self.assertEqual(len(t_a.sent_recv3), 1)
        self.assertEqual(t_b.sent_recv3, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
