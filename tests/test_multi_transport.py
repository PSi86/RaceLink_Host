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

    def test_resolves_via_direct_network_id_binding(self):
        # A transport stamped with ``network_id`` resolves directly,
        # without needing a gateway_mac on the network — the path an
        # Ethernet transport (host NIC, no gateway MAC) relies on.
        from racelink.domain.models import RL_Network

        host = self._make_controller()

        class _FakeTransport:
            def __init__(self, nid):
                self.ident_mac = None
                self.network_id = nid

        net = RL_Network(name="Stage LAN", kind="ethernet", gateway_mac=None)
        t = _FakeTransport(net.id)
        host._transports = [_FakeTransport("other-net"), t]
        host.network_repository.append(net)

        self.assertIs(host.transport_for_network(net.id), t)

    def test_direct_network_id_binding_takes_precedence_over_mac(self):
        # When both a direct network_id stamp and a gateway_mac would
        # match different transports, the direct stamp wins.
        from racelink.domain.models import RL_Network

        host = self._make_controller()

        class _FakeTransport:
            def __init__(self, mac=None, nid=None):
                self.ident_mac = mac
                self.network_id = nid

        net = RL_Network(name="Track A", gateway_mac="AA:BB:CC:DD:EE:02")
        t_mac = _FakeTransport(mac="AA:BB:CC:DD:EE:02", nid=None)
        t_direct = _FakeTransport(mac=None, nid=net.id)
        host._transports = [t_mac, t_direct]
        host.network_repository.append(net)

        self.assertIs(host.transport_for_network(net.id), t_direct)


class EthernetTransportAttachTests(unittest.TestCase):
    """Ethernet PoC: ``_attach_ethernet_transports`` binds one UDP transport
    per ``kind="ethernet"`` network, routes via the stamped network_id, and
    keeps the host ``ready`` even with no RF gateway present."""

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

    def test_attach_routes_and_ready_without_rf(self):
        from racelink.domain.models import RL_Network

        host = self._make_controller()
        # The runtime state repository is a process-wide singleton, so isolate
        # this test's network set (and restore it afterwards) to keep the
        # attach count deterministic regardless of leftovers from sibling tests.
        repo = host.network_repository
        original = list(repo.list())
        # host_port=0 -> OS-assigned ephemeral port, no cross-test clash.
        net = RL_Network(
            name="Stage LAN", kind="ethernet",
            eth_config={"host_port": 0, "bind_host": "127.0.0.1"},
        )
        repo.replace_all([net])

        try:
            count = host._attach_ethernet_transports()
            self.assertEqual(count, 1)
            self.assertEqual(len(host.transports), 1)
            t = host.transports[0]
            self.assertEqual(getattr(t, "kind", None), "ethernet")
            self.assertEqual(t.network_id, net.id)
            # Routing resolves via the directly-stamped network_id.
            self.assertIs(host.transport_for_network(net.id), t)

            # No RF gateway found -> Ethernet keeps the host ready.
            host._handle_no_rf_gateway(
                reason="none", origin="programmatic", code="NOT_FOUND",
            )
            self.assertTrue(host.ready)
        finally:
            host._close_all_transports()
            repo.replace_all(original)


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


class AttachTransportInstallsServiceHooksTests(unittest.TestCase):
    """Bench-test #3 regression guard: ``_attach_transport`` must
    install ``gateway_service.on_transport_event`` on EVERY attached
    transport — not just the first. The pre-fix bug had the controller
    calling ``_install_transport_hooks()`` without a transport arg,
    which defaulted to ``self.transport`` (= ``_transports[0]``) and
    therefore never wired a listener onto secondary transports. RX
    events on the second gateway then bypassed device-state updates,
    pending-matcher correlation, and EV_ERROR disconnect handling."""

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

    def _make_fake_transport(self, ident_mac, port):
        class _FakeTransport:
            def __init__(self, mac, p):
                self.ident_mac = mac
                self.port = p
                self.listeners = []
                self.tx_listeners = []

            def start(self):
                # No serial port to open in tests.
                pass

            def add_listener(self, cb):
                self.listeners.append(cb)

            def add_tx_listener(self, cb):
                self.tx_listeners.append(cb)

        return _FakeTransport(ident_mac, port)

    def test_each_transport_gets_service_rx_listener(self):
        host = self._make_controller()
        # Detach any bind service so evaluate() doesn't try to probe
        # GW_CMD_GET_RF_CONFIG on the fake transports.
        host.gateway_bind_service = None
        t1 = self._make_fake_transport("AA:BB:CC:DD:EE:01", "/dev/ttyFAKE1")
        t2 = self._make_fake_transport("AA:BB:CC:DD:EE:02", "/dev/ttyFAKE2")

        host._attach_transport(t1)
        host._attach_transport(t2)

        on_event = host.gateway_service.on_transport_event
        self.assertIn(
            on_event, t1.listeners,
            "primary transport must carry the service's on_transport_event listener",
        )
        self.assertIn(
            on_event, t2.listeners,
            "secondary transport must ALSO carry the service's on_transport_event "
            "listener — the pre-fix controller wrapper defaulted to _transports[0] "
            "and left the second transport silent on RX (bench-test #3 regression).",
        )

    def test_install_hooks_idempotent_per_transport(self):
        # Re-running _install_transport_hooks for the same transport
        # must not duplicate the listener — the installed_set guard
        # in gateway_service.install_transport_hooks is keyed by
        # ``id(transport)``.
        host = self._make_controller()
        host.gateway_bind_service = None
        t = self._make_fake_transport("AA:BB:CC:DD:EE:01", "/dev/ttyFAKE1")
        host._attach_transport(t)
        host._install_transport_hooks(t)  # explicit re-install
        on_event = host.gateway_service.on_transport_event
        # Use ``==`` (not ``is``) because Python creates a fresh
        # bound-method object on every attribute access; the listener
        # stored on the transport equals the freshly-fetched method
        # by __self__/__func__ identity but not by ``is``.
        self.assertEqual(
            sum(1 for cb in t.listeners if cb == on_event), 1,
            "duplicate listener after re-install — installed_set tracking broken",
        )


class PerTransportEvErrorCleanupTests(unittest.TestCase):
    """Task 2 (per-transport disconnect): when one of N transports
    emits EV_ERROR, drop only THAT transport and broadcast
    ``gateway_detached`` so the WebUI removes it from its pills.
    Last-transport death keeps the legacy global ``schedule_reconnect``
    path."""

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

    def _make_fake_transport(self, ident_mac, port):
        class _FakeTransport:
            def __init__(self, mac, p):
                self.ident_mac = mac
                self.port = p
                self.closed = False

            def close(self):
                self.closed = True

        return _FakeTransport(ident_mac, port)

    def _capture_broadcasts(self, host):
        events: list[tuple[str, dict]] = []

        def _broadcast(name, payload):
            events.append((name, dict(payload or {})))

        host.gateway_bind_service._broadcast = _broadcast
        return events

    def test_per_transport_cleanup_when_multiple_attached(self):
        from racelink.services.gateway_service import EV_ERROR

        host = self._make_controller()
        events = self._capture_broadcasts(host)

        t_alive = self._make_fake_transport("AA:AA:AA:AA:AA:01", "/dev/ttyFAKE0")
        t_dead = self._make_fake_transport("BB:BB:BB:BB:BB:02", "/dev/ttyFAKE1")
        host._transports = [t_alive, t_dead]

        # Pretend the dead transport's read thread emits EV_ERROR
        # tagged with its ident_mac.
        host.gateway_service.on_transport_event({
            "type": EV_ERROR,
            "data": "USB unplug",
            "gateway_id": "BB:BB:BB:BB:BB:02",
        })

        # close() runs on a daemon thread (the EV_ERROR handler runs in
        # the dying rx thread; close() would otherwise try to join its
        # own thread → RuntimeError). Wait briefly for it to fire.
        import time as _time
        deadline = _time.time() + 1.0
        while not t_dead.closed and _time.time() < deadline:
            _time.sleep(0.01)

        # The dead transport was closed, dropped from the list, and the
        # surviving transport stays attached and untouched.
        self.assertTrue(t_dead.closed)
        self.assertFalse(t_alive.closed)
        self.assertEqual(host._transports, [t_alive])
        # No global teardown: controller.ready is unchanged (still
        # True after default init), no reconnect thread was scheduled.
        self.assertFalse(getattr(host, "_reconnect_in_progress", False))
        # Frontend gets gateway_detached so the pill goes away.
        detached_events = [(n, p) for n, p in events if n == "gateway_detached"]
        self.assertEqual(len(detached_events), 1)
        self.assertEqual(detached_events[0][1]["ident_mac"], "BB:BB:BB:BB:BB:02")
        self.assertEqual(detached_events[0][1]["reason"], "USB unplug")

    def test_last_transport_death_uses_per_transport_cleanup(self):
        """Round 5 follow-up: even at N=1, EV_ERROR with a known
        gateway_id takes the per-transport cleanup path. The old
        global ``schedule_reconnect`` path used to fire here but
        left the dead transport in ``_transports`` (soft_rediscover
        in the reconnect handler skipped it as 'already attached')
        — visible as ``controller.ready=False`` stuck on, and the
        legacy "Gateway is not available" banner persisting until
        manual page refresh."""
        from racelink.services.gateway_service import EV_ERROR

        host = self._make_controller()
        events = self._capture_broadcasts(host)

        t_solo = self._make_fake_transport("AA:AA:AA:AA:AA:01", "/dev/ttyFAKE0")
        host._transports = [t_solo]

        host.gateway_service.on_transport_event({
            "type": EV_ERROR,
            "data": "USB unplug",
            "gateway_id": "AA:AA:AA:AA:AA:01",
        })

        # Per-transport cleanup ran: the dead transport was removed
        # from _transports, and a gateway_detached SSE event fired.
        self.assertEqual(host._transports, [])
        detached_events = [(n, p) for n, p in events if n == "gateway_detached"]
        self.assertEqual(len(detached_events), 1)
        self.assertEqual(detached_events[0][1]["ident_mac"], "AA:AA:AA:AA:AA:01")

    def test_untagged_ev_error_falls_back_to_global_path(self):
        """Pre-handshake transport failure: no ident_mac stamped on
        the event. The dispatcher cannot identify the dead transport
        unambiguously, so it falls through to the global reconnect
        path which closes and re-enumerates everything."""
        from racelink.services.gateway_service import EV_ERROR

        host = self._make_controller()
        events = self._capture_broadcasts(host)

        t1 = self._make_fake_transport("AA:AA:AA:AA:AA:01", "/dev/ttyFAKE0")
        t2 = self._make_fake_transport("BB:BB:BB:BB:BB:02", "/dev/ttyFAKE1")
        host._transports = [t1, t2]

        host.gateway_service.on_transport_event({
            "type": EV_ERROR,
            "data": "pre-handshake failure",
            # no gateway_id stamped
        })

        # Global path: controller.ready flipped, no per-transport
        # detached broadcast.
        self.assertFalse(host.ready)
        detached_events = [(n, p) for n, p in events if n == "gateway_detached"]
        self.assertEqual(detached_events, [])


class MissingTransportTrackerTests(unittest.TestCase):
    """Round 3 Task 2: the tracker arms a 5s poll whenever any
    ``RL_Network.gateway_mac`` is missing from ``controller._transports``
    AND not on the operator's cancel list; clears the timer when the
    missing set is empty; broadcasts ``gateway_missing`` snapshots."""

    def _make_tracker(self):
        from racelink.services.missing_transport_tracker import MissingTransportTracker

        # Lightweight controller fake — only the attributes the tracker
        # touches. ``soft_rediscover`` is a stub that pushes a faked
        # transport into _transports so the tracker can flip from
        # "missing" to "found" without needing real USB.
        class _FakeTransport:
            def __init__(self, mac):
                self.ident_mac = mac

        class _Network:
            def __init__(self, id, name, gateway_mac):
                self.id = id
                self.name = name
                self.gateway_mac = gateway_mac

        class _Repo:
            def __init__(self):
                self.items: list = []

            def list(self):
                return list(self.items)

            def get_by_gateway_mac(self, mac):
                if not mac:
                    return None
                target = str(mac).upper()
                for n in self.items:
                    if str(getattr(n, "gateway_mac", "") or "").upper() == target:
                        return n
                return None

        class _Controller:
            def __init__(self):
                self.network_repository = _Repo()
                self._transports: list = []
                self.rediscovery_calls = 0
                self._rediscover_attach_macs: list[str] = []

            def soft_rediscover(self):
                self.rediscovery_calls += 1
                # Attach any MACs the test scripted for this call.
                for mac in self._rediscover_attach_macs:
                    self._transports.append(_FakeTransport(mac))
                self._rediscover_attach_macs = []
                return len(self._transports)

        ctrl = _Controller()
        broadcasts: list[tuple[str, dict]] = []

        def _bc(name, payload):
            broadcasts.append((name, dict(payload or {})))

        # 0.1s poll keeps tests fast.
        tracker = MissingTransportTracker(
            controller=ctrl, poll_interval_s=0.1, broadcast=_bc,
        )
        return ctrl, tracker, broadcasts, _Network

    def test_no_arm_when_nothing_missing(self):
        ctrl, tracker, broadcasts, Network = self._make_tracker()
        # Network exists and matching transport is attached.
        ctrl.network_repository.items.append(Network("n1", "A", "AA:AA:01"))
        class _T:
            ident_mac = "AA:AA:01"
        ctrl._transports.append(_T())
        tracker.evaluate_and_arm()
        self.assertEqual(tracker.missing_macs(), set())
        # A "missing=[]" broadcast still goes out so the WebUI can
        # clear any stale banner.
        self.assertTrue(any(n == "gateway_missing" and not p["missing"] for n, p in broadcasts))
        tracker.shutdown()

    def test_arm_and_fire_attaches_then_clears(self):
        import time as _time
        ctrl, tracker, broadcasts, Network = self._make_tracker()
        ctrl.network_repository.items.append(Network("n1", "A", "AA:AA:01"))
        # No transport yet → missing.
        tracker.evaluate_and_arm()
        self.assertIn("AA:AA:01", tracker.missing_macs())
        first_broadcast = [
            p for n, p in broadcasts if n == "gateway_missing" and p["missing"]
        ][0]
        self.assertEqual(first_broadcast["missing"][0]["ident_mac"], "AA:AA:01")
        self.assertEqual(first_broadcast["missing"][0]["network_name"], "A")
        # Script the next soft_rediscover to attach the missing MAC.
        ctrl._rediscover_attach_macs = ["AA:AA:01"]
        # Wait for the timer to fire (poll_interval_s=0.1 + small slack).
        deadline = _time.time() + 1.0
        while ctrl.rediscovery_calls == 0 and _time.time() < deadline:
            _time.sleep(0.02)
        self.assertGreaterEqual(ctrl.rediscovery_calls, 1)
        # After the poll the missing set is empty + a clearing
        # broadcast fired.
        self.assertEqual(tracker.missing_macs(), set())
        clearing = [p for n, p in broadcasts if n == "gateway_missing" and not p["missing"]]
        self.assertTrue(clearing)
        tracker.shutdown()

    def test_cancel_suppresses_polling(self):
        ctrl, tracker, broadcasts, Network = self._make_tracker()
        ctrl.network_repository.items.append(Network("n1", "A", "AA:AA:01"))
        ctrl.network_repository.items.append(Network("n2", "B", "BB:BB:02"))
        tracker.evaluate_and_arm()
        self.assertEqual(tracker.missing_macs(), {"AA:AA:01", "BB:BB:02"})
        # Cancel just one.
        tracker.cancel("AA:AA:01")
        self.assertEqual(tracker.missing_macs(), {"BB:BB:02"})
        self.assertIn("AA:AA:01", tracker.cancelled_macs())
        # Cancel ALL (None).
        tracker.cancel(None)
        self.assertEqual(tracker.missing_macs(), set())
        self.assertEqual(tracker.cancelled_macs(), {"AA:AA:01", "BB:BB:02"})
        tracker.shutdown()

    def test_clear_cancelled_re_arms_missing(self):
        ctrl, tracker, broadcasts, Network = self._make_tracker()
        ctrl.network_repository.items.append(Network("n1", "A", "AA:AA:01"))
        tracker.cancel("AA:AA:01")
        self.assertEqual(tracker.missing_macs(), set())
        tracker.clear_cancelled()
        self.assertEqual(tracker.missing_macs(), {"AA:AA:01"})
        tracker.shutdown()

    def test_snapshot_carries_network_metadata(self):
        ctrl, tracker, broadcasts, Network = self._make_tracker()
        ctrl.network_repository.items.append(Network("net-uuid-7", "Pit-Lane", "AA:AA:01"))
        tracker.evaluate_and_arm()
        snap = tracker.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["ident_mac"], "AA:AA:01")
        self.assertEqual(snap[0]["network_id"], "net-uuid-7")
        self.assertEqual(snap[0]["network_name"], "Pit-Lane")
        # next_retry_in_s is set because the timer was armed.
        self.assertIsNotNone(snap[0]["next_retry_in_s"])
        tracker.shutdown()


class AttachTransportRound4Tests(unittest.TestCase):
    """Round 4 Tasks 1+2: a successful ``_attach_transport`` clears the
    global gateway-retry timer (no more dueling reconnect paths) and
    rejects a duplicate ident_mac (no more double-attach race)."""

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

    def _make_fake_transport(self, ident_mac, port):
        class _FakeTransport:
            def __init__(self, mac, p):
                self.ident_mac = mac
                self.port = p
                self.listeners: list = []
                self.tx_listeners: list = []
                self.closed = False

            def start(self):
                pass

            def close(self):
                self.closed = True

            def add_listener(self, cb):
                self.listeners.append(cb)

            def add_tx_listener(self, cb):
                self.tx_listeners.append(cb)

        return _FakeTransport(ident_mac, port)

    def test_attach_cancels_pending_retry_timer(self):
        host = self._make_controller()
        host.gateway_bind_service = None
        # Arm a retry timer directly (mirrors the auto-eligible
        # LINK_LOST / NOT_FOUND path used in production).
        host.last_gateway_error = {"reason": "simulated", "code": "LINK_LOST"}
        host._schedule_gateway_retry(60.0)
        self.assertIsNotNone(host._gateway_retry_timer)
        # A subsequent attach must cancel it (so the soft_rediscover
        # success doesn't race the scheduled discoverPort retry).
        t = self._make_fake_transport("AA:AA:AA:AA:AA:01", "/dev/ttyFAKE0")
        host._attach_transport(t)
        self.assertIsNone(host._gateway_retry_timer)
        self.assertIsNone(host.last_gateway_error)

    def test_attach_rejects_duplicate_ident_mac(self):
        import time as _time

        host = self._make_controller()
        host.gateway_bind_service = None
        t1 = self._make_fake_transport("AA:AA:AA:AA:AA:01", "/dev/ttyFAKE0")
        host._attach_transport(t1)
        self.assertEqual(len(host._transports), 1)

        # Same ident_mac on a fresh transport instance — the soft_-
        # rediscover / discoverPort race scenario. The latecomer must
        # be closed and dropped; _transports stays at length 1.
        t2 = self._make_fake_transport("AA:AA:AA:AA:AA:01", "/dev/ttyFAKE0")
        host._attach_transport(t2)
        self.assertEqual(len(host._transports), 1)
        self.assertIs(host._transports[0], t1)

        # close() ran on a daemon thread.
        deadline = _time.time() + 1.0
        while not t2.closed and _time.time() < deadline:
            _time.sleep(0.01)
        self.assertTrue(t2.closed)


class ScheduleReconnectGracefulPathTests(unittest.TestCase):
    """Round 4 Task 3: ``schedule_reconnect`` no longer nukes every
    transport when at least one is still attached. It calls
    ``soft_rediscover()`` for the graceful path; the nuclear
    ``_close_all_transports + discoverPort`` is reserved for N=0."""

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

    def test_soft_path_when_transports_attached(self):
        import time as _time

        host = self._make_controller()
        host.gateway_bind_service = None
        host.missing_transport_tracker = None

        class _FakeTransport:
            ident_mac = "AA:AA:AA:AA:AA:01"
        host._transports.append(_FakeTransport())

        soft_calls: list = []
        close_calls: list = []
        discover_calls: list = []

        def _soft():
            soft_calls.append(_time.time())
            return 0

        def _close():
            close_calls.append(_time.time())

        def _discover(_args, origin=None):
            discover_calls.append(origin)

        host.soft_rediscover = _soft
        host._close_all_transports = _close
        host.discoverPort = _discover

        host.gateway_service.schedule_reconnect("test reason")
        # Reconnect runs on a daemon thread.
        deadline = _time.time() + 1.0
        while not soft_calls and _time.time() < deadline:
            _time.sleep(0.01)

        self.assertEqual(len(soft_calls), 1)
        self.assertEqual(close_calls, [])
        self.assertEqual(discover_calls, [])

    def test_nuclear_path_when_no_transports(self):
        import time as _time

        host = self._make_controller()
        host.gateway_bind_service = None
        host.missing_transport_tracker = None
        # No transports attached → nuclear path.
        self.assertEqual(host._transports, [])

        soft_calls: list = []
        close_calls: list = []
        discover_calls: list = []

        def _soft():
            soft_calls.append(True)
            return 0

        def _close():
            close_calls.append(True)

        def _discover(_args, origin=None):
            discover_calls.append(origin)

        host.soft_rediscover = _soft
        host._close_all_transports = _close
        host.discoverPort = _discover

        host.gateway_service.schedule_reconnect("zero-transport reason")
        deadline = _time.time() + 1.0
        while not discover_calls and _time.time() < deadline:
            _time.sleep(0.01)

        self.assertEqual(soft_calls, [])
        self.assertEqual(len(close_calls), 1)
        self.assertEqual(discover_calls, ["auto"])


class FormatGatewayLabelTests(unittest.TestCase):
    """Round 5 follow-up: ``controller.format_gateway_label`` builds
    a compact ``[#N XXXX/NetworkName]`` string so multi-gateway debug
    traces show which gateway emitted each log line. Falls back to
    ``[? unknown]`` for empty / pre-handshake ident_macs."""

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

        host = RaceLink_Host(_FakeHostApi(), "test", "Test")
        # The state_repository is a process-wide singleton; clear it
        # to isolate this test from any earlier-test mutations.
        host.network_repository.replace_all([])
        return host

    def test_unknown_ident_returns_fallback(self):
        host = self._make_controller()
        self.assertEqual(host.format_gateway_label(""), "[? unknown]")
        self.assertEqual(host.format_gateway_label(None), "[? unknown]")

    def test_label_with_index_and_network_name(self):
        from racelink.domain.models import RL_Network

        host = self._make_controller()

        class _FakeTransport:
            def __init__(self, mac):
                self.ident_mac = mac

        host._transports = [
            _FakeTransport("AA:BB:CC:DD:EE:01"),
            _FakeTransport("AA:BB:CC:DD:EE:02"),
        ]
        host.network_repository.append(
            RL_Network(name="Pit-Lane", gateway_mac="AA:BB:CC:DD:EE:02"),
        )

        label = host.format_gateway_label("AA:BB:CC:DD:EE:02")
        # Index = 1 (second in _transports), MAC suffix = last 4 hex,
        # network = Pit-Lane.
        self.assertEqual(label, "[#1 EE02/Pit-Lane]")

    def test_label_without_network_uses_mac_only(self):
        host = self._make_controller()

        class _FakeTransport:
            def __init__(self, mac):
                self.ident_mac = mac

        host._transports = [_FakeTransport("AA:BB:CC:DD:EE:01")]
        # No matching RL_Network with this gateway_mac.
        label = host.format_gateway_label("AA:BB:CC:DD:EE:01")
        self.assertEqual(label, "[#0 EE01]")

    def test_label_for_detached_gateway_still_shows_mac(self):
        host = self._make_controller()
        # No transport attached, but the operator wants to see WHICH
        # gateway just disconnected in the log.
        label = host.format_gateway_label("AA:BB:CC:DD:EE:01")
        self.assertEqual(label, "[EE01]")


class EnumerateAllExcludePortsTests(unittest.TestCase):
    """Round 5 follow-up: ``enumerate_all`` must skip ports the caller
    already owns. Probing a /dev/ttyUSB device with an active
    GatewaySerialTransport corrupts the live USB-CDC stream — the
    IDENTIFY probe payload lands on the gateway's normal command
    channel and the existing reader sees garbage, eventually firing
    EV_ERROR (the cascading-disconnect pattern from bench-test #6)."""

    def test_exclude_ports_skips_listed_devices(self):
        # We monkey-patch ``serial.tools.list_ports.comports`` to return
        # a fixed two-port list, then assert that the excluded port is
        # NEVER opened by enumerate_all.
        import serial.tools.list_ports as _ports
        from racelink.transport.gateway_serial import GatewaySerialTransport

        class _FakePortInfo:
            def __init__(self, device, description):
                self.device = device
                self.description = description

        fake_ports = [
            _FakePortInfo("/dev/ttyFAKE0", "USB Serial"),
            _FakePortInfo("/dev/ttyFAKE1", "USB Serial"),
        ]
        opened: list[str] = []

        class _FakeSerial:
            def __init__(self, *_a, **_kw):
                self.port = None
                self.baudrate = 0
                self.is_open = False
                self.exclusive = False

            def open(self):
                opened.append(self.port)
                self.is_open = True

            def close(self):
                self.is_open = False

            def reset_input_buffer(self):
                pass

            def write(self, _data):
                pass

            def read(self, _n):
                return b""  # no match -> skipped

        original_comports = _ports.comports
        original_serial = __import__("serial").Serial
        _ports.comports = lambda: fake_ports
        import serial as _serial_mod
        _serial_mod.Serial = _FakeSerial
        try:
            GatewaySerialTransport.enumerate_all(
                exclude_ports={"/dev/ttyFAKE0"},
            )
        finally:
            _ports.comports = original_comports
            _serial_mod.Serial = original_serial

        # Excluded port must NOT have been opened — that's the
        # operating contract that prevents the cascade.
        self.assertNotIn("/dev/ttyFAKE0", opened)
        # Non-excluded port was probed normally.
        self.assertIn("/dev/ttyFAKE1", opened)


class QueryGatewayRfConfigRetryTests(unittest.TestCase):
    """Round 4 Task 5: ``query_gateway_rf_config`` retries once after
    a 200 ms settle if the first attempt times out. Default timeout is
    1.5 s (was 0.5 s) — fast enough at boot, generous enough to ride
    out post-open USB-CDC stabilisation."""

    def _make_gateway_service(self):
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
        return host, host.gateway_service

    class _ScriptedTransport:
        """Fake transport that scripts which attempts succeed.

        ``replies_on_attempts`` is a sequence like ``[False, True]`` —
        first attempt no reply, second attempt fires EV_RF_CHANGED.
        """
        def __init__(self, replies_on_attempts):
            self._replies = list(replies_on_attempts)
            self._attempt = 0
            self.ident_mac = "AA:AA:AA:AA:AA:01"
            self.port = "/dev/ttyFAKE"
            self._listeners: list = []
            self.send_calls = 0

        def add_listener(self, cb):
            self._listeners.append(cb)

        def remove_listener(self, cb):
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass

        def send_get_rf_config(self) -> bool:
            self.send_calls += 1
            should_reply = (
                self._attempt < len(self._replies)
                and self._replies[self._attempt]
            )
            self._attempt += 1
            if should_reply:
                # Fire EV_RF_CHANGED synchronously to the registered
                # listener so the waiter.set() unblocks immediately.
                from racelink.transport.gateway_events import EV_RF_CHANGED
                ev = {
                    "type": EV_RF_CHANGED,
                    "rf_config": {
                        "freq_hz": 867_700_000,
                        "bw_khz_x10": 1250,
                        "sf": 7,
                        "cr_den": 5,
                        "sync_word": 0x12,
                        "tx_power_dbm": 14,
                        "preamble": 8,
                    },
                    "reason": 0,
                }
                for cb in list(self._listeners):
                    try:
                        cb(ev)
                    except Exception:
                        pass
            return True

    def test_first_attempt_succeeds_no_retry(self):
        _host, gw = self._make_gateway_service()
        t = self._ScriptedTransport(replies_on_attempts=[True])
        result = gw.query_gateway_rf_config(transport=t, timeout_s=0.2)
        self.assertTrue(result.get("ok"))
        self.assertIn("rf_config", result)
        self.assertEqual(t.send_calls, 1)

    def test_timeout_then_retry_succeeds(self):
        _host, gw = self._make_gateway_service()
        t = self._ScriptedTransport(replies_on_attempts=[False, True])
        result = gw.query_gateway_rf_config(transport=t, timeout_s=0.1)
        self.assertTrue(result.get("ok"))
        # send_get_rf_config was invoked twice (one for each attempt).
        self.assertEqual(t.send_calls, 2)

    def test_both_attempts_timeout_returns_failure(self):
        _host, gw = self._make_gateway_service()
        t = self._ScriptedTransport(replies_on_attempts=[False, False])
        result = gw.query_gateway_rf_config(transport=t, timeout_s=0.1)
        self.assertFalse(result.get("ok"))
        self.assertIn("error", result)
        self.assertEqual(t.send_calls, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
