"""Scene-runner cross-network routing tests (Stage 3 Part G).

A scene whose actions target groups on different networks must
land each frame on the right radio. The runner itself is unchanged
— it dispatches via ``ControlService`` / ``SyncService``. The
routing change is in those services: group sends resolve via
``controller.transport_for_group`` and device sends via
``controller.transport_for_device``.

Pins:

  * ``send_group_preset`` and ``send_offset(targetGroup=...)`` route
    via the group's network's transport (not the singleton).
  * ``send_control(targetGroup=...)`` does the same.
  * A scene's ``sync`` action fans out across every transport (the
    Stage-3 broadcast-sync contract from Part G).
  * The boundary enforcement from Part B still rejects cross-
    network membership BEFORE we ever get here — these tests
    don't re-verify that rule.

The test suite stops short of spinning up the full
:class:`SceneRunner`; the unit coverage on the control-service /
sync-service routing is what matters for Stage 3 Part G. The
runner is exercised end-to-end in the existing scenes test
suite without changes — its dispatcher just calls the same
service methods.
"""

from __future__ import annotations

import unittest

from racelink.domain.models import RL_Device, RL_DeviceGroup, RL_Network
from racelink.services.control_service import ControlService
from racelink.services.gateway_service import GatewayService


class _FakeTransport:
    """Records every wire call. Covers send_preset / send_offset /
    send_control / send_sync — the four primitives the scene runner
    + control_service + sync_service hit."""

    def __init__(self, ident_mac):
        self.ident_mac = ident_mac
        self.preset_calls: list[dict] = []
        self.offset_calls: list[dict] = []
        self.control_calls: list[dict] = []
        self.sync_calls: list[dict] = []
        self.listeners: list = []
        self.tx_listeners: list = []

    # Sends -----------------------------------------------------------
    def send_preset(self, *, recv3, group_id, flags, preset_id, brightness):
        self.preset_calls.append({
            "recv3": bytes(recv3), "group_id": int(group_id),
            "flags": int(flags), "preset_id": int(preset_id),
            "brightness": int(brightness),
        })

    def send_offset(self, *, recv3, group_id, mode, **kwargs):
        self.offset_calls.append({
            "recv3": bytes(recv3), "group_id": int(group_id),
            "mode": mode, "kwargs": dict(kwargs),
        })

    def send_control(self, *, recv3, group_id, flags, **kwargs):
        self.control_calls.append({
            "recv3": bytes(recv3), "group_id": int(group_id),
            "flags": int(flags), "kwargs": dict(kwargs),
        })

    def send_sync(self, *, recv3, ts24, brightness, flags):
        self.sync_calls.append({
            "recv3": bytes(recv3), "ts24": int(ts24),
            "brightness": int(brightness), "flags": int(flags),
        })

    # Hooks-only stubs the control-service / gateway-service touch -----
    def add_listener(self, cb):
        if cb not in self.listeners:
            self.listeners.append(cb)

    def remove_listener(self, cb):
        if cb in self.listeners:
            self.listeners.remove(cb)

    def add_tx_listener(self, cb):
        if cb not in self.tx_listeners:
            self.tx_listeners.append(cb)


class _Repo:
    def __init__(self, items):
        self._items = list(items)

    def list(self):
        return list(self._items)

    def append(self, item):
        self._items.append(item)
        return item

    def get_by_id(self, network_id):
        for n in self._items:
            if str(getattr(n, "id", "") or "") == str(network_id or ""):
                return n
        return None

    def get_by_gateway_mac(self, gateway_mac):
        if not gateway_mac:
            return None
        target = str(gateway_mac).upper()
        for n in self._items:
            if str(getattr(n, "gateway_mac", "") or "").upper() == target:
                return n
        return None

    def get_by_addr(self, addr):
        target = str(addr or "").upper()
        if not target:
            return None
        for d in self._items:
            addr_up = str(getattr(d, "addr", "") or "").upper()
            if addr_up == target:
                return d
            if len(target) == 6 and addr_up.endswith(target):
                return d
        return None


class _FakeController:
    def __init__(self, *, networks, groups, devices, transports):
        self.network_repository = _Repo(networks)
        self.group_repository = _Repo(groups)
        self.device_repository = _Repo(devices)
        self._transports = list(transports)
        self._transport_hooks_installed_for: set[int] = set()

    @property
    def transports(self):
        return list(self._transports)

    @property
    def transport(self):
        return self._transports[0] if self._transports else None

    def getDeviceFromAddress(self, mac):
        return self.device_repository.get_by_addr(mac)

    def transport_for_network(self, network_id):
        net = self.network_repository.get_by_id(network_id)
        if net is None or not getattr(net, "gateway_mac", None):
            return self._transports[0] if len(self._transports) == 1 else None
        target = str(net.gateway_mac).upper()
        for t in self._transports:
            if str(getattr(t, "ident_mac", "") or "").upper() == target:
                return t
        return None

    def transport_for_device(self, addr):
        dev = self.device_repository.get_by_addr(addr)
        net_id = getattr(dev, "network_id", None) if dev is not None else None
        return self.transport_for_network(net_id)

    def transport_for_group(self, group_id):
        try:
            gid_int = int(group_id) & 0xFF
        except (TypeError, ValueError):
            return self._transports[0] if len(self._transports) == 1 else None
        groups = self.group_repository.list()
        if 0 <= gid_int < len(groups):
            net_id = getattr(groups[gid_int], "network_id", None)
            routed = self.transport_for_network(net_id) if net_id else None
            if routed is not None:
                return routed
        return self._transports[0] if len(self._transports) == 1 else None


def _make_services(*, transports, networks, groups, devices):
    ctrl = _FakeController(
        networks=networks, groups=groups, devices=devices,
        transports=transports,
    )
    gw = GatewayService(ctrl)
    ctrl.gateway_service = gw  # type: ignore[attr-defined]
    cs = ControlService(controller=ctrl, gateway_service=gw)
    return ctrl, gw, cs


class GroupSendRoutingTests(unittest.TestCase):

    def test_send_group_preset_routes_via_groups_network(self):
        net_a = RL_Network(name="A", gateway_mac="GW-A")
        net_b = RL_Network(name="B", gateway_mac="GW-B")
        # Group ids 0..3: 0 Unconfigured / 1 on net-a / 2 on net-b /
        # 3 "All WLED Nodes" (static).
        groups = [
            RL_DeviceGroup(name="Unconfigured", static_group=1, dev_type=0,
                           network_id=net_a.id),
            RL_DeviceGroup(name="Team A", static_group=0, dev_type=0,
                           network_id=net_a.id),
            RL_DeviceGroup(name="Team B", static_group=0, dev_type=0,
                           network_id=net_b.id),
        ]
        t_a = _FakeTransport("GW-A")
        t_b = _FakeTransport("GW-B")
        _ctrl, _gw, cs = _make_services(
            transports=[t_a, t_b], networks=[net_a, net_b],
            groups=groups, devices=[],
        )

        ok = cs.send_group_preset(group_id=2, flags=0xC0, preset_id=5, brightness=64)

        self.assertTrue(ok)
        # Only t_b (net-b's gateway) received the preset frame.
        self.assertEqual(len(t_b.preset_calls), 1)
        self.assertEqual(t_b.preset_calls[0]["group_id"], 2)
        self.assertEqual(t_b.preset_calls[0]["recv3"], b"\xFF\xFF\xFF")
        self.assertEqual(t_a.preset_calls, [])

    def test_send_offset_to_group_routes_via_groups_network(self):
        net_a = RL_Network(name="A", gateway_mac="GW-A")
        net_b = RL_Network(name="B", gateway_mac="GW-B")
        groups = [
            RL_DeviceGroup(name="Unconfigured", static_group=1, dev_type=0,
                           network_id=net_a.id),
            RL_DeviceGroup(name="Team A", static_group=0, dev_type=0,
                           network_id=net_a.id),
            RL_DeviceGroup(name="Team B", static_group=0, dev_type=0,
                           network_id=net_b.id),
        ]
        t_a = _FakeTransport("GW-A")
        t_b = _FakeTransport("GW-B")
        _ctrl, _gw, cs = _make_services(
            transports=[t_a, t_b], networks=[net_a, net_b],
            groups=groups, devices=[],
        )

        cs.send_offset(targetGroup=1, mode="linear", base_ms=100, step_ms=50)

        self.assertEqual(len(t_a.offset_calls), 1)
        self.assertEqual(t_a.offset_calls[0]["group_id"], 1)
        self.assertEqual(t_b.offset_calls, [])

    def test_send_control_to_group_routes_via_groups_network(self):
        net_a = RL_Network(name="A", gateway_mac="GW-A")
        net_b = RL_Network(name="B", gateway_mac="GW-B")
        groups = [
            RL_DeviceGroup(name="Unconfigured", static_group=1, dev_type=0,
                           network_id=net_a.id),
            RL_DeviceGroup(name="Team A", static_group=0, dev_type=0,
                           network_id=net_a.id),
            RL_DeviceGroup(name="Team B", static_group=0, dev_type=0,
                           network_id=net_b.id),
        ]
        t_a = _FakeTransport("GW-A")
        t_b = _FakeTransport("GW-B")
        _ctrl, _gw, cs = _make_services(
            transports=[t_a, t_b], networks=[net_a, net_b],
            groups=groups, devices=[],
        )

        cs.send_control(targetGroup=2, params={"mode": 5, "brightness": 80})

        self.assertEqual(len(t_b.control_calls), 1)
        self.assertEqual(t_b.control_calls[0]["group_id"], 2)
        self.assertEqual(t_a.control_calls, [])


class DeviceSendRoutingTests(unittest.TestCase):

    def test_send_device_preset_routes_via_devices_network(self):
        net_a = RL_Network(name="A", gateway_mac="GW-A")
        net_b = RL_Network(name="B", gateway_mac="GW-B")
        dev_b = RL_Device("AABBCC222222", dev_type=0, name="dev-b", groupId=2)
        dev_b.network_id = net_b.id
        t_a = _FakeTransport("GW-A")
        t_b = _FakeTransport("GW-B")
        _ctrl, _gw, cs = _make_services(
            transports=[t_a, t_b], networks=[net_a, net_b],
            groups=[], devices=[dev_b],
        )

        cs.send_device_preset(dev_b, flags=0xC0, preset_id=3, brightness=20)

        self.assertEqual(len(t_b.preset_calls), 1)
        self.assertEqual(t_b.preset_calls[0]["recv3"], b"\x22\x22\x22")
        self.assertEqual(t_a.preset_calls, [])

    def test_send_control_to_device_routes_via_devices_network(self):
        net_a = RL_Network(name="A", gateway_mac="GW-A")
        net_b = RL_Network(name="B", gateway_mac="GW-B")
        dev_b = RL_Device("AABBCC222222", dev_type=0, name="dev-b", groupId=2)
        dev_b.network_id = net_b.id
        t_a = _FakeTransport("GW-A")
        t_b = _FakeTransport("GW-B")
        _ctrl, _gw, cs = _make_services(
            transports=[t_a, t_b], networks=[net_a, net_b],
            groups=[], devices=[dev_b],
        )

        cs.send_control(targetDevice=dev_b, params={"mode": 4, "brightness": 30})

        self.assertEqual(len(t_b.control_calls), 1)
        self.assertEqual(t_b.control_calls[0]["recv3"], b"\x22\x22\x22")
        self.assertEqual(t_a.control_calls, [])


class SceneSyncFanOutContractTests(unittest.TestCase):
    """A scene's ``sync`` action ultimately hits
    ``SyncService.send_sync`` → ``GatewayService.send_sync``. The
    fan-out at the gateway-service layer (tested in detail in
    :mod:`tests.test_sync_multi_network_fanout`) is what the
    scene runner relies on for cross-network sync ticks."""

    def test_scene_sync_reaches_every_transport(self):
        from racelink.services.sync_service import SyncService

        t_a = _FakeTransport("GW-A")
        t_b = _FakeTransport("GW-B")
        ctrl = _FakeController(
            networks=[RL_Network(name="A", gateway_mac="GW-A"),
                      RL_Network(name="B", gateway_mac="GW-B")],
            groups=[], devices=[], transports=[t_a, t_b],
        )
        gw = GatewayService(ctrl)
        ctrl.gateway_service = gw  # type: ignore[attr-defined]
        sync = SyncService(controller=ctrl, gateway_service=gw)

        sync.send_sync(ts24=0xABCDEF, brightness=12, trigger_armed=True)

        self.assertEqual(len(t_a.sync_calls), 1)
        self.assertEqual(len(t_b.sync_calls), 1)
        self.assertEqual(t_a.sync_calls[0]["ts24"], 0xABCDEF)
        self.assertEqual(t_b.sync_calls[0]["ts24"], 0xABCDEF)
        # trigger_armed flows through to both.
        self.assertEqual(t_a.sync_calls[0]["flags"] & 0x01, 0x01)
        self.assertEqual(t_b.sync_calls[0]["flags"] & 0x01, 0x01)


class FallbackTests(unittest.TestCase):
    """Single-transport deployments and legacy (un-migrated) groups
    fall back to the singleton transport — N=1 behaviour stays
    byte-identical."""

    def test_n1_single_transport_unchanged(self):
        net = RL_Network(name="Default", gateway_mac="GW-A")
        groups = [
            RL_DeviceGroup(name="Unconfigured", static_group=1, dev_type=0,
                           network_id=net.id),
            RL_DeviceGroup(name="Team", static_group=0, dev_type=0,
                           network_id=net.id),
        ]
        t = _FakeTransport("GW-A")
        _ctrl, _gw, cs = _make_services(
            transports=[t], networks=[net], groups=groups, devices=[],
        )

        cs.send_group_preset(group_id=1, flags=0xC0, preset_id=2, brightness=64)
        self.assertEqual(len(t.preset_calls), 1)

    def test_legacy_group_without_network_id_falls_back(self):
        # Single transport, group without network_id — routing helper
        # returns the singleton.
        groups = [
            RL_DeviceGroup(name="Unconfigured", static_group=1, dev_type=0),
            RL_DeviceGroup(name="Legacy Team", static_group=0, dev_type=0),
        ]
        t = _FakeTransport("GW-A")
        _ctrl, _gw, cs = _make_services(
            transports=[t], networks=[], groups=groups, devices=[],
        )
        cs.send_group_preset(group_id=1, flags=0xC0, preset_id=2, brightness=64)
        self.assertEqual(len(t.preset_calls), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
