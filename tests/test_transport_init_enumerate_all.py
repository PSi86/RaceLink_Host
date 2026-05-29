"""Multi-transport boot tests (Stage 2 Part 5).

Pins three contracts:

  1. ``discoverPort`` walks :meth:`GatewaySerialTransport.enumerate_all`
     (when no ``rl_comms_port`` hint is set) and attaches one
     transport per discovered gateway. At N=1 hardware the end state
     is identical to the pre-Part-5 single-transport path.
  2. ``_bind_transport_to_network`` follows the Part-5 policy:
     gateway_mac match → bind; single-transport + unbound network →
     auto-bind and persist the MAC; otherwise stay unbound with a
     WARNING.
  3. ``_close_all_transports`` releases every attached transport's
     OS file-descriptor lock so a fresh enumeration probe doesn't
     see ``PORT_BUSY`` on its own siblings.
"""

from __future__ import annotations

import unittest
from unittest import mock

from racelink import controller as controller_module
from racelink.controller import RaceLink_Host
from racelink.domain.models import RL_Network


class _FakeHostApi:
    class _Db:
        def __init__(self):
            self._opts: dict = {}

        def option(self, key, default=None):
            return self._opts.get(key, default)

        def set_option(self, key, value):
            self._opts[key] = value

    def __init__(self):
        self.db = _FakeHostApi._Db()

    def fire_event(self, *_a, **_kw):
        pass

    def log(self, *_a, **_kw):
        pass


class _FakeTransport:
    """Stand-in for :class:`GatewaySerialTransport` that records each
    lifecycle call without touching a real serial port."""

    instances: list = []

    def __init__(self, *, port=None, on_event=None):
        self.port = port
        self.on_event = on_event
        self.ident_mac = None
        self.opened = False
        self.started = False
        self.closed = False
        self._listeners: list = []
        self._tx_listeners: list = []
        self.last_discovery_had_busy_port = False
        _FakeTransport.instances.append(self)

    def open(self):
        self.opened = True

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def discover_and_open(self):
        # Manual-pin path: the constructor was given a concrete port and
        # ``discover_and_open`` would just open it. Always succeed for
        # the test path that exercises ``rl_comms_port``.
        self.opened = True
        return True

    def add_listener(self, cb):
        if cb not in self._listeners:
            self._listeners.append(cb)

    def add_tx_listener(self, cb):
        if cb not in self._tx_listeners:
            self._tx_listeners.append(cb)


def _make_controller():
    # Use a fresh ``StateRepository`` per test so the module-level
    # ``get_runtime_state_repository`` singleton's network_repository
    # doesn't accumulate cross-test residue (the auto-bind lookup
    # walks every entry, and a leftover network with the same MAC
    # would shadow the one this test appends).
    from racelink.state.repository import StateRepository

    host = RaceLink_Host(
        _FakeHostApi(), "test", "Test", state_repository=StateRepository(),
    )
    return host


class EnumerateAllBootPathTests(unittest.TestCase):

    def setUp(self):
        _FakeTransport.instances.clear()

    def test_two_gateways_attach_two_transports(self):
        host = _make_controller()
        # Seed the network repository with two networks bound to known
        # MACs so the auto-bind step finds an exact match for each.
        net_a = RL_Network(name="Track A", gateway_mac="AA:BB:CC:DD:EE:01")
        net_b = RL_Network(name="Track B", gateway_mac="AA:BB:CC:DD:EE:02")
        host.network_repository.append(net_a)
        host.network_repository.append(net_b)

        enumerated = [
            ("COM3", "AA:BB:CC:DD:EE:01"),
            ("COM4", "AA:BB:CC:DD:EE:02"),
        ]
        with mock.patch.object(
            controller_module, "GatewaySerialTransport", _FakeTransport,
        ), mock.patch.object(
            _FakeTransport, "enumerate_all",
            staticmethod(lambda: list(enumerated)),
            create=True,
        ):
            host.discoverPort({}, origin="programmatic")

        self.assertTrue(host.ready)
        self.assertEqual(len(host.transports), 2)
        ports = sorted(t.port for t in host.transports)
        self.assertEqual(ports, ["COM3", "COM4"])
        idents = sorted(t.ident_mac for t in host.transports)
        self.assertEqual(idents, ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"])
        # Each transport was opened, then started.
        for t in host.transports:
            self.assertTrue(t.opened)
            self.assertTrue(t.started)
        # Auto-bind stamped network_id from the existing repo entries.
        bound = {t.ident_mac: getattr(t, "network_id", None) for t in host.transports}
        self.assertEqual(bound["AA:BB:CC:DD:EE:01"], net_a.id)
        self.assertEqual(bound["AA:BB:CC:DD:EE:02"], net_b.id)

    def test_single_gateway_path_remains_byte_identical(self):
        host = _make_controller()
        # No networks pre-seeded — ``load_from_db`` was not invoked in
        # this lean test, so the auto-bind policy can't fire. The
        # behaviour at N=1 still mirrors the pre-Part-5 path: one
        # transport opened, started, ``ready=True``.
        enumerated = [("COM7", "AA:BB:CC:DD:EE:42")]
        with mock.patch.object(
            controller_module, "GatewaySerialTransport", _FakeTransport,
        ), mock.patch.object(
            _FakeTransport, "enumerate_all",
            staticmethod(lambda: list(enumerated)),
            create=True,
        ):
            host.discoverPort({}, origin="programmatic")

        self.assertTrue(host.ready)
        self.assertEqual(len(host.transports), 1)
        self.assertEqual(host.transport.port, "COM7")
        self.assertEqual(host.transport.ident_mac, "AA:BB:CC:DD:EE:42")

    def test_enumeration_returns_no_gateways_records_error(self):
        host = _make_controller()
        with mock.patch.object(
            controller_module, "GatewaySerialTransport", _FakeTransport,
        ), mock.patch.object(
            _FakeTransport, "enumerate_all",
            staticmethod(lambda: []),
            create=True,
        ):
            host.discoverPort({}, origin="auto")

        self.assertFalse(host.ready)
        self.assertEqual(host.transports, [])
        # ``last_gateway_error`` is set by ``_record_gateway_error``.
        self.assertIsNotNone(host.last_gateway_error)
        self.assertEqual(host.last_gateway_error.get("code"), "NOT_FOUND")

    def test_rl_comms_port_hint_still_uses_legacy_path(self):
        host = _make_controller()
        # Pin a port — the legacy single-port path opens that and only
        # that, never calls enumerate_all.
        host._host_api.db.set_option("rl_comms_port", "COM9")
        enumerate_calls = []

        def _fake_enumerate():
            enumerate_calls.append(True)
            return []

        with mock.patch.object(
            controller_module, "GatewaySerialTransport", _FakeTransport,
        ), mock.patch.object(
            _FakeTransport, "enumerate_all",
            staticmethod(_fake_enumerate),
            create=True,
        ):
            host.discoverPort({}, origin="manual")

        self.assertTrue(host.ready)
        self.assertEqual(len(host.transports), 1)
        self.assertEqual(host.transport.port, "COM9")
        self.assertEqual(enumerate_calls, [])

    def test_multi_pin_attaches_only_whitelisted_ports(self):
        host = _make_controller()
        # Multi-pin: enumerate, then attach only the pinned ports. The
        # unpinned COM4 is enumerated but filtered out.
        host._host_api.db.set_option("rl_comms_port", "COM3,COM5")
        enumerated = [
            ("COM3", "AA:BB:CC:DD:EE:01"),
            ("COM4", "AA:BB:CC:DD:EE:02"),
            ("COM5", "AA:BB:CC:DD:EE:03"),
        ]
        with mock.patch.object(
            controller_module, "GatewaySerialTransport", _FakeTransport,
        ), mock.patch.object(
            _FakeTransport, "enumerate_all",
            staticmethod(lambda: list(enumerated)),
            create=True,
        ):
            host.discoverPort({}, origin="programmatic")

        self.assertTrue(host.ready)
        self.assertEqual(sorted(t.port for t in host.transports), ["COM3", "COM5"])
        self.assertEqual(
            sorted(t.ident_mac for t in host.transports),
            ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:03"],
        )

    def test_multi_pin_no_match_records_not_found(self):
        host = _make_controller()
        host._host_api.db.set_option("rl_comms_port", "COM98,COM99")
        enumerated = [("COM3", "AA:BB:CC:DD:EE:01")]
        with mock.patch.object(
            controller_module, "GatewaySerialTransport", _FakeTransport,
        ), mock.patch.object(
            _FakeTransport, "enumerate_all",
            staticmethod(lambda: list(enumerated)),
            create=True,
        ):
            host.discoverPort({}, origin="auto")

        self.assertFalse(host.ready)
        self.assertEqual(host.transports, [])
        self.assertEqual(host.last_gateway_error.get("code"), "NOT_FOUND")

    def test_close_all_transports_drops_every_slot(self):
        host = _make_controller()
        # Manually attach two fakes (skipping discoverPort) so we can
        # verify the close path on its own.
        t_a = _FakeTransport(port="COM3")
        t_a.ident_mac = "AA"
        t_b = _FakeTransport(port="COM4")
        t_b.ident_mac = "BB"
        host._transports = [t_a, t_b]

        host._close_all_transports()
        self.assertEqual(host._transports, [])
        self.assertTrue(t_a.closed)
        self.assertTrue(t_b.closed)


class AutoBindPolicyTests(unittest.TestCase):
    """``_bind_transport_to_network`` — the three resolution branches
    of the Stage-2 binding policy in isolation."""

    def test_exact_gateway_mac_match_binds_existing_network(self):
        host = _make_controller()
        net = RL_Network(name="Track A", gateway_mac="AA:BB:CC")
        host.network_repository.append(net)
        t = _FakeTransport(port="COM3")
        t.ident_mac = "AA:BB:CC"
        host._transports = [t]
        bound = host._bind_transport_to_network(t)
        self.assertEqual(bound, net.id)
        # And the back-reference on the transport is stamped.
        self.assertEqual(getattr(t, "network_id", None), net.id)

    def test_single_transport_unbound_network_auto_binds_and_persists(self):
        host = _make_controller()
        # Default network with no gateway_mac (v1->v2 freshly migrated).
        net = RL_Network(name="Default", gateway_mac=None)
        host.network_repository.append(net)
        t = _FakeTransport(port="COM3")
        t.ident_mac = "DE:AD:BE:EF"
        host._transports = [t]
        # Spy on save_to_db so we can verify the persistence step fires.
        with mock.patch.object(host, "save_to_db") as save:
            bound = host._bind_transport_to_network(t)
        self.assertEqual(bound, net.id)
        self.assertEqual(net.gateway_mac, "DE:AD:BE:EF")
        save.assert_called_once()
        self.assertEqual(getattr(t, "network_id", None), net.id)

    def test_multi_transport_unknown_gateway_stays_unbound(self):
        host = _make_controller()
        net = RL_Network(name="Track A", gateway_mac="AA:BB:CC")
        host.network_repository.append(net)
        # Two transports attached, second one has no matching network.
        # The Stage-2 policy refuses to auto-bind because the
        # ambiguity (which network gets the new MAC?) needs the
        # Stage-3 wizard.
        t_a = _FakeTransport(port="COM3")
        t_a.ident_mac = "AA:BB:CC"
        t_b = _FakeTransport(port="COM4")
        t_b.ident_mac = "FF:FF:FF"
        host._transports = [t_a, t_b]
        bound_a = host._bind_transport_to_network(t_a)
        bound_b = host._bind_transport_to_network(t_b)
        self.assertEqual(bound_a, net.id)
        self.assertIsNone(bound_b)
        # No new gateway_mac persisted on the existing network.
        self.assertEqual(net.gateway_mac, "AA:BB:CC")

    def test_transport_without_ident_mac_stays_unbound(self):
        host = _make_controller()
        net = RL_Network(name="Default", gateway_mac=None)
        host.network_repository.append(net)
        t = _FakeTransport(port="COM3")
        # ident_mac stays None — pinned port without an IDENTIFY
        # round-trip yet.
        host._transports = [t]
        bound = host._bind_transport_to_network(t)
        self.assertIsNone(bound)
        # Network's gateway_mac is untouched.
        self.assertIsNone(net.gateway_mac)


class NormalizeCommsPinsTests(unittest.TestCase):
    """``_normalize_comms_pins`` — the rl_comms_port value parser that
    feeds the single/multi-pin branches of ``discoverPort``."""

    def test_value_forms(self):
        f = RaceLink_Host._normalize_comms_pins
        self.assertEqual(f(None), [])
        self.assertEqual(f(""), [])
        self.assertEqual(f("   "), [])
        self.assertEqual(f("COM12"), ["COM12"])
        self.assertEqual(f("COM12,COM13"), ["COM12", "COM13"])
        self.assertEqual(f(" COM12 , COM13 "), ["COM12", "COM13"])
        self.assertEqual(f("COM12,,"), ["COM12"])
        self.assertEqual(f(["COM12", "COM13"]), ["COM12", "COM13"])
        self.assertEqual(f('["COM12","COM13"]'), ["COM12", "COM13"])
        self.assertEqual(f("/dev/ttyUSB0,/dev/ttyACM0"),
                         ["/dev/ttyUSB0", "/dev/ttyACM0"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
