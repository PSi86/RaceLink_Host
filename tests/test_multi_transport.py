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
        # gateway_id=None (default) -> the matcher accepts events from
        # any transport, including untagged events. Pure single-gateway
        # backwards-compat path.
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
