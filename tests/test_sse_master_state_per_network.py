"""Per-network ``MasterState`` tests (Stage 2 Part 4).

Pins the multi-network wire contract:

  1. ``MasterStateMap`` maintains one ``MasterState`` per network and
     exposes ``snapshot()`` as ``{networks: [...], default_network_id}``.
  2. ``SSEBridge.on_transport_event`` routes ``EV_STATE_CHANGED`` /
     ``EV_STATE_REPORT`` to the slot owned by the source transport
     (via ``ev["gateway_id"]`` → ``RL_Network.gateway_mac`` →
     ``RL_Network.id``).
  3. Untagged events (legacy single-gateway code path) still land in
     the default network — N=1 deployments stay byte-identical.
  4. The bridge hooks every transport on the controller, not just
     ``controller.transport``.
"""

from __future__ import annotations

import unittest

from racelink.state.migrations import DEFAULT_NETWORK_ID
from racelink.transport import (
    EV_STATE_CHANGED,
    EV_STATE_REPORT,
    GATEWAY_STATE_IDLE,
    GATEWAY_STATE_NAME,
    GATEWAY_STATE_RX_WINDOW,
)
from racelink.web.sse import MasterState, MasterStateMap, SSEBridge


class _FakeNetwork:
    def __init__(self, id, *, gateway_mac=None, name="Network"):
        self.id = id
        self.gateway_mac = gateway_mac
        self.name = name


class _FakeNetworkRepository:
    def __init__(self, nets=None):
        self._items = list(nets or [])

    def list(self):
        return list(self._items)

    def get_by_gateway_mac(self, gateway_mac):
        if not gateway_mac:
            return None
        target = str(gateway_mac).upper()
        for net in self._items:
            if str(getattr(net, "gateway_mac", "") or "").upper() == target:
                return net
        return None


class _FakeController:
    def __init__(self, nets=None, transports=None):
        self.network_repository = _FakeNetworkRepository(nets or [])
        self._transports = list(transports or [])

    @property
    def transports(self):
        return self._transports

    @property
    def transport(self):
        return self._transports[0] if self._transports else None


class _FakeTransport:
    def __init__(self, ident_mac=None):
        self.ident_mac = ident_mac
        self._listeners = []

    def add_listener(self, cb):
        if cb not in self._listeners:
            self._listeners.append(cb)


class MasterStateMapTests(unittest.TestCase):

    def test_default_slot_exists_before_attach_controller(self):
        broadcasts = []
        m = MasterStateMap(lambda ev, payload: broadcasts.append((ev, payload)))
        snap = m.snapshot()
        self.assertEqual(snap["default_network_id"], DEFAULT_NETWORK_ID)
        # Exactly one entry — the eagerly-created default slot.
        self.assertEqual(len(snap["networks"]), 1)
        self.assertEqual(snap["networks"][0]["network_id"], DEFAULT_NETWORK_ID)
        self.assertEqual(snap["networks"][0]["state"], "UNKNOWN")

    def test_attach_controller_picks_up_first_network_as_default(self):
        broadcasts = []
        m = MasterStateMap(lambda ev, payload: broadcasts.append((ev, payload)))
        net_a = _FakeNetwork("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                             gateway_mac="AA:BB:CC:DD:EE:01", name="Track A")
        net_b = _FakeNetwork("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                             gateway_mac="AA:BB:CC:DD:EE:02", name="Track B")
        m.attach_controller(_FakeController(nets=[net_a, net_b]))
        self.assertEqual(m.default_network_id, net_a.id)
        snap = m.snapshot()
        ids = [row["network_id"] for row in snap["networks"]]
        self.assertEqual(ids, [net_a.id, net_b.id])
        self.assertEqual(snap["networks"][0]["name"], "Track A")
        self.assertEqual(snap["networks"][1]["name"], "Track B")

    def test_for_gateway_resolves_via_repo(self):
        broadcasts = []
        m = MasterStateMap(lambda ev, payload: broadcasts.append((ev, payload)))
        net_a = _FakeNetwork("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                             gateway_mac="AA:BB:CC:DD:EE:01")
        net_b = _FakeNetwork("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                             gateway_mac="AA:BB:CC:DD:EE:02")
        m.attach_controller(_FakeController(nets=[net_a, net_b]))

        ms_a = m.for_gateway("AA:BB:CC:DD:EE:01")
        ms_b = m.for_gateway("AA:BB:CC:DD:EE:02")
        self.assertIsInstance(ms_a, MasterState)
        self.assertIsInstance(ms_b, MasterState)
        self.assertIsNot(ms_a, ms_b)

        # Unknown gateway_id -> default
        ms_unknown = m.for_gateway("ZZ:ZZ:ZZ:ZZ:ZZ:99")
        self.assertIs(ms_unknown, m.default)
        # None gateway_id -> default
        self.assertIs(m.for_gateway(None), m.default)

    def test_per_state_set_emits_multinetwork_payload(self):
        broadcasts = []
        m = MasterStateMap(lambda ev, payload: broadcasts.append((ev, payload)))
        net_a = _FakeNetwork("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                             gateway_mac="AA")
        net_b = _FakeNetwork("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                             gateway_mac="BB")
        m.attach_controller(_FakeController(nets=[net_a, net_b]))

        m.for_gateway("BB").apply_gateway_state(GATEWAY_STATE_RX_WINDOW, 250,
                                                source_event="STATE_CHANGED")
        # The broadcast is the unified multi-network shape, not the
        # per-state snapshot.
        self.assertTrue(broadcasts)
        last_event, last_payload = broadcasts[-1]
        self.assertEqual(last_event, "master")
        self.assertIn("networks", last_payload)
        ids = [row["network_id"] for row in last_payload["networks"]]
        self.assertIn(net_b.id, ids)
        b_row = next(row for row in last_payload["networks"]
                     if row["network_id"] == net_b.id)
        self.assertEqual(b_row["state"],
                         GATEWAY_STATE_NAME.get(GATEWAY_STATE_RX_WINDOW))
        # The other network was not touched.
        a_row = next(row for row in last_payload["networks"]
                     if row["network_id"] == net_a.id)
        self.assertEqual(a_row["state"], "UNKNOWN")


class SSEBridgeMultiTransportTests(unittest.TestCase):

    def test_master_is_default_network_state(self):
        bridge = SSEBridge(logger=None)
        self.assertIs(bridge.master, bridge.masters.default)

    def test_state_event_routes_to_owning_network(self):
        bridge = SSEBridge(logger=None)
        net_a = _FakeNetwork("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                             gateway_mac="GW-A", name="A")
        net_b = _FakeNetwork("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                             gateway_mac="GW-B", name="B")
        ctrl = _FakeController(nets=[net_a, net_b])
        bridge.attach_controller(ctrl)

        # Tagged event from GW-B should update only the B-slot.
        bridge.on_transport_event({
            "type": EV_STATE_CHANGED,
            "state_byte": GATEWAY_STATE_RX_WINDOW,
            "state_metadata_ms": 100,
            "gateway_id": "GW-B",
        })
        snap = bridge.masters.snapshot()
        rows = {row["network_id"]: row for row in snap["networks"]}
        self.assertEqual(rows[net_b.id]["state"],
                         GATEWAY_STATE_NAME.get(GATEWAY_STATE_RX_WINDOW))
        self.assertEqual(rows[net_a.id]["state"], "UNKNOWN")

    def test_untagged_state_event_falls_back_to_default(self):
        """Legacy single-gateway path (events without ``gateway_id``)
        still updates the default-network slot — N=1 deployments stay
        byte-identical."""
        bridge = SSEBridge(logger=None)
        bridge.on_transport_event({
            "type": EV_STATE_REPORT,
            "state_byte": 0,  # IDLE
            "state_metadata_ms": 0,
        })
        snap = bridge.masters.snapshot()
        # default slot, single entry (no controller attached)
        self.assertEqual(len(snap["networks"]), 1)
        self.assertEqual(snap["networks"][0]["state"], "IDLE")
        self.assertEqual(snap["networks"][0]["network_id"],
                         DEFAULT_NETWORK_ID)

    def test_ensure_transport_hooked_attaches_every_transport(self):
        bridge = SSEBridge(logger=None)
        t_a = _FakeTransport(ident_mac="GW-A")
        t_b = _FakeTransport(ident_mac="GW-B")
        ctrl = _FakeController(transports=[t_a, t_b])
        bridge.ensure_transport_hooked(ctrl)
        # Both transports received the listener.
        self.assertIn(bridge.on_transport_event, t_a._listeners)
        self.assertIn(bridge.on_transport_event, t_b._listeners)
        # Idempotent — re-calling doesn't double up.
        bridge.ensure_transport_hooked(ctrl)
        self.assertEqual(t_a._listeners.count(bridge.on_transport_event), 1)
        self.assertEqual(t_b._listeners.count(bridge.on_transport_event), 1)

    def test_rebind_transport_re_hooks_after_clear(self):
        bridge = SSEBridge(logger=None)
        t_a = _FakeTransport(ident_mac="GW-A")
        ctrl = _FakeController(transports=[t_a])
        bridge.ensure_transport_hooked(ctrl)
        # Simulate a transport recycle: new instance, same controller.
        t_a2 = _FakeTransport(ident_mac="GW-A")
        ctrl._transports = [t_a2]
        bridge.rebind_transport(ctrl)
        self.assertIn(bridge.on_transport_event, t_a2._listeners)


class _FakeEthTransport:
    """Mimics an attached EthernetTransport for the seed-on-hook path."""

    kind = "ethernet"

    def __init__(self, network_id, state_byte=GATEWAY_STATE_IDLE):
        self.network_id = network_id
        self.ident_mac = f"ETH:{network_id}"
        self.gateway_state_byte = state_byte
        self.gateway_state_metadata_ms = 0
        self._listeners = []

    def add_listener(self, cb):
        if cb not in self._listeners:
            self._listeners.append(cb)

    def gateway_state_snapshot(self):
        return {
            "state_byte": self.gateway_state_byte,
            "state": GATEWAY_STATE_NAME.get(self.gateway_state_byte, "UNKNOWN"),
            "state_metadata_ms": 0,
        }


class EthernetMasterStateTests(unittest.TestCase):
    """Ethernet PoC: the host NIC presents as a ready 'gateway'."""

    def test_for_gateway_routes_eth_ident_to_network_slot(self):
        m = MasterStateMap(lambda ev, payload: None)
        # No repo lookup needed — the network id is encoded in the ident.
        slot = m.for_gateway("ETH:net-eth-1")
        self.assertIsInstance(slot, MasterState)
        self.assertIsNot(slot, m.default)
        # Same ident resolves to the same slot.
        self.assertIs(m.for_gateway("ETH:net-eth-1"), slot)
        # A different ethernet network gets a distinct slot.
        self.assertIsNot(m.for_gateway("ETH:net-eth-2"), slot)

    def test_hooking_ethernet_transport_seeds_idle(self):
        bridge = SSEBridge(logger=None)
        t_eth = _FakeEthTransport("net-eth-1")
        ctrl = _FakeController(transports=[t_eth])
        bridge.ensure_transport_hooked(ctrl)
        # Listener attached + master slot seeded to IDLE (not UNKNOWN).
        self.assertIn(bridge.on_transport_event, t_eth._listeners)
        snap = bridge.masters.snapshot()
        rows = {row["network_id"]: row for row in snap["networks"]}
        self.assertIn("net-eth-1", rows)
        self.assertEqual(rows["net-eth-1"]["state"], "IDLE")

    def test_rf_transport_without_state_is_not_seeded(self):
        # A plain transport reporting no state (UNKNOWN) must not be
        # force-seeded — RF stays UNKNOWN until its first STATE_REPORT.
        bridge = SSEBridge(logger=None)
        t_rf = _FakeTransport(ident_mac="GW-A")  # no gateway_state_byte attr
        ctrl = _FakeController(transports=[t_rf])
        bridge.ensure_transport_hooked(ctrl)
        snap = bridge.masters.snapshot()
        # Only the eagerly-created default slot, still UNKNOWN.
        self.assertEqual(snap["networks"][0]["state"], "UNKNOWN")


class MasterMapBackCompatTests(unittest.TestCase):
    """The legacy ``last_event`` diagnostic updates from TaskManager and
    ``api.py`` still flow through ``bridge.master`` and are visible in
    the default-network row of the multi-network snapshot."""

    def test_legacy_master_set_appears_in_default_row(self):
        bridge = SSEBridge(logger=None)
        bridge.master.set(last_event="TASK_DISCOVER_START")
        snap = bridge.masters.snapshot()
        default_row = next(
            row for row in snap["networks"]
            if row["network_id"] == bridge.masters.default_network_id
        )
        self.assertEqual(default_row["last_event"], "TASK_DISCOVER_START")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
