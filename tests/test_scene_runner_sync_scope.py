"""Scene runner sync-target wiring tests.

Pins:

  * When the scene's non-sync actions resolve to a strict subset of
    networks, the scene's ``sync`` action only reaches that subset —
    uninvolved networks see NO sync packet (so they can't accidentally
    fire armed effects).
  * When the scene targets every network (e.g. group=255 broadcast),
    the sync reaches every transport.
  * When the scene has no resolvable network membership (sync-only
    scene or only-unknown targets), the sync falls back to all
    attached transports (deprecated default).
"""

from __future__ import annotations

import unittest

from racelink.domain.models import RL_Device, RL_DeviceGroup, RL_Network
from racelink.services.control_service import ControlService
from racelink.services.gateway_service import GatewayService
from racelink.services.scene_runner_service import SceneRunnerService
from racelink.services.sync_service import SyncService


class _FakeTransport:
    def __init__(self, ident_mac):
        self.ident_mac = ident_mac
        self.preset_calls: list = []
        self.sync_calls: list = []
        self.listeners: list = []
        self.tx_listeners: list = []

    def send_preset(self, *, recv3, group_id, flags, preset_id, brightness):
        self.preset_calls.append({
            "recv3": bytes(recv3), "group_id": int(group_id),
            "flags": int(flags), "preset_id": int(preset_id),
            "brightness": int(brightness),
        })

    def send_sync(self, *, recv3, ts24, brightness, flags):
        self.sync_calls.append({
            "recv3": bytes(recv3), "ts24": int(ts24),
            "brightness": int(brightness), "flags": int(flags),
        })

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

    def get_by_id(self, network_id):
        for n in self._items:
            if str(getattr(n, "id", "") or "") == str(network_id or ""):
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

    def get_by_gateway_mac(self, gateway_mac):
        if not gateway_mac:
            return None
        target = str(gateway_mac).upper()
        for n in self._items:
            if str(getattr(n, "gateway_mac", "") or "").upper() == target:
                return n
        return None


class _FakeScenesService:
    def __init__(self, scenes):
        self._scenes = dict(scenes)

    def get(self, scene_key):
        return self._scenes.get(scene_key)


class _FakeController:
    def __init__(self, *, networks, groups, devices, transports):
        self.network_repository = _Repo(networks)
        self.group_repository = _Repo(groups)
        self.device_repository = _Repo(devices)
        self._transports = list(transports)
        self._transport_hooks_installed_for: set = set()

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


def _build_runner(*, networks, groups, devices, transports, scenes):
    ctrl = _FakeController(
        networks=networks, groups=groups,
        devices=devices, transports=transports,
    )
    gw = GatewayService(ctrl)
    ctrl.gateway_service = gw  # type: ignore[attr-defined]
    cs = ControlService(controller=ctrl, gateway_service=gw)
    sync = SyncService(controller=ctrl, gateway_service=gw)
    runner = SceneRunnerService(
        controller=ctrl,
        scenes_service=_FakeScenesService(scenes),
        control_service=cs,
        sync_service=sync,
        sleep=lambda s: None,
        clock_ms=lambda: 0,
    )
    return ctrl, runner


def _two_network_fixture(*, scene_actions, transport_macs=("GW-A", "GW-B")):
    net_a = RL_Network(name="A", gateway_mac=transport_macs[0])
    net_b = RL_Network(name="B", gateway_mac=transport_macs[1])
    # gid 0 = Unconfigured/net-A, gid 1 = Team A/net-A, gid 2 = Team B/net-B.
    groups = [
        RL_DeviceGroup(name="Unconfigured", static_group=1, dev_type=0,
                       network_id=net_a.id),
        RL_DeviceGroup(name="Team A", static_group=0, dev_type=0,
                       network_id=net_a.id),
        RL_DeviceGroup(name="Team B", static_group=0, dev_type=0,
                       network_id=net_b.id),
    ]
    t_a = _FakeTransport(transport_macs[0])
    t_b = _FakeTransport(transport_macs[1])
    scene = {
        "key": "test-scene",
        "actions": list(scene_actions),
        "stop_on_error": False,
    }
    ctrl, runner = _build_runner(
        networks=[net_a, net_b],
        groups=groups,
        devices=[],
        transports=[t_a, t_b],
        scenes={"test-scene": scene},
    )
    return ctrl, runner, t_a, t_b, net_a, net_b


class SceneSyncScopeTests(unittest.TestCase):

    def test_sync_scoped_to_subset_of_networks(self):
        """Scene with a group-A preset + a sync action should only fire
        the sync on network A's transport — network B sees nothing."""
        scene_actions = [
            {"kind": "rl_preset",
             "target": {"kind": "groups", "value": [1]},
             "params": {"presetId": 5, "brightness": 64}},
            {"kind": "sync"},
        ]
        _ctrl, runner, t_a, t_b, _, _ = _two_network_fixture(
            scene_actions=scene_actions,
        )

        result = runner.run("test-scene")
        # Note: rl_preset planner emits a send_wled_preset op which
        # uses send_group_preset → routed to net_a's transport.
        self.assertTrue(result.ok or any(r.ok for r in result.actions))

        # The sync should ONLY have hit t_a — t_b's network is not
        # part of the scene's scope.
        self.assertEqual(len(t_a.sync_calls), 1, "expected sync on net_a")
        self.assertEqual(t_b.sync_calls, [],
                         "expected NO sync on net_b (uninvolved network)")

    def test_sync_reaches_all_networks_when_broadcast_target(self):
        """A scene action with target.kind=broadcast pulls every
        network into scope; sync hits all of them."""
        scene_actions = [
            {"kind": "rl_preset",
             "target": {"kind": "broadcast"},
             "params": {"presetId": 1, "brightness": 32}},
            {"kind": "sync"},
        ]
        _ctrl, runner, t_a, t_b, _, _ = _two_network_fixture(
            scene_actions=scene_actions,
        )

        runner.run("test-scene")
        self.assertEqual(len(t_a.sync_calls), 1)
        self.assertEqual(len(t_b.sync_calls), 1)

    def test_sync_only_scene_falls_back_to_all_attached(self):
        """No non-sync actions → no resolvable scope → SYNC falls back
        to the deprecated 'all attached' default (with warning)."""
        scene_actions = [
            {"kind": "sync"},
        ]
        _ctrl, runner, t_a, t_b, _, _ = _two_network_fixture(
            scene_actions=scene_actions,
        )

        runner.run("test-scene")
        # Both transports got the sync because target=None
        # → fallback to all_attached.
        self.assertEqual(len(t_a.sync_calls), 1)
        self.assertEqual(len(t_b.sync_calls), 1)

    def test_unicast_sync_unaffected_by_scope(self):
        """Sanity check: unicast SYNC (recv3 != FFFFFF) still routes
        via the device's network and ignores scene scope. The scene-
        runner path always emits broadcast SYNC, but a direct
        sync.send_sync(recv3=...) call from a future caller should
        keep working."""
        dev_b = RL_Device("AABBCC222222", dev_type=0, name="dev-b", groupId=2)
        net_a = RL_Network(name="A", gateway_mac="GW-A")
        net_b = RL_Network(name="B", gateway_mac="GW-B")
        dev_b.network_id = net_b.id

        t_a = _FakeTransport("GW-A")
        t_b = _FakeTransport("GW-B")
        ctrl = _FakeController(
            networks=[net_a, net_b], groups=[],
            devices=[dev_b], transports=[t_a, t_b],
        )
        gw = GatewayService(ctrl)
        sync = SyncService(controller=ctrl, gateway_service=gw)

        sync.send_sync(ts24=42, brightness=0, recv3=b"\x22\x22\x22")

        self.assertEqual(t_a.sync_calls, [])
        self.assertEqual(len(t_b.sync_calls), 1)


class ExplicitScopeRunnerTests(unittest.TestCase):
    """Verify the operator-pinned ``network_scope`` field flows through
    the runner to every broadcast opcode (SYNC, PRESET, CONTROL,
    OFFSET) — not just SYNC."""

    def test_explicit_scope_narrows_sync_to_subset(self):
        """A scene with a broadcast action + explicit scope=[net-A]
        fires SYNC only on net-A, even though the broadcast action
        would otherwise pull every network into scope."""
        scene_actions = [
            {"kind": "wled_preset",
             "target": {"kind": "broadcast"},
             "params": {"presetId": 1, "brightness": 32}},
            {"kind": "sync"},
        ]
        _ctrl, runner, t_a, t_b, net_a, _ = _two_network_fixture(
            scene_actions=scene_actions,
        )
        # Override the scene's persisted scope to explicit-[net_a].
        scene = runner.scenes_service._scenes["test-scene"]  # noqa: SLF001
        scene["network_scope"] = {
            "mode": "explicit",
            "network_ids": [net_a.id],
        }

        runner.run("test-scene")
        # SYNC only on net_a (the explicit scope).
        self.assertEqual(len(t_a.sync_calls), 1)
        self.assertEqual(t_b.sync_calls, [],
                         "explicit scope must keep SYNC off uninvolved networks")
        # The broadcast WLED preset also reaches only net_a's transport
        # (this is the runner-wiring-completeness check from B5: non-SYNC
        # broadcast opcodes also respect the scene's scope).
        self.assertEqual(len(t_a.preset_calls), 1)
        self.assertEqual(t_b.preset_calls, [])

    def test_explicit_scope_fanned_out_preset(self):
        """A scene with a broadcast WLED preset + explicit scope =
        [net-A, net-B] reaches BOTH gateways. This catches the prior
        send_wled_preset gap where target= was stripped before it
        reached send_group_preset."""
        scene_actions = [
            {"kind": "wled_preset",
             "target": {"kind": "broadcast"},
             "params": {"presetId": 5, "brightness": 64}},
        ]
        _ctrl, runner, t_a, t_b, net_a, net_b = _two_network_fixture(
            scene_actions=scene_actions,
        )
        scene = runner.scenes_service._scenes["test-scene"]  # noqa: SLF001
        scene["network_scope"] = {
            "mode": "explicit",
            "network_ids": [net_a.id, net_b.id],
        }

        runner.run("test-scene")
        # Broadcast WLED preset reaches both gateways via the threaded
        # fan-out (group_id=255 + target=BroadcastTarget(...)).
        self.assertEqual(len(t_a.preset_calls), 1)
        self.assertEqual(len(t_b.preset_calls), 1)

    def test_stale_scope_id_resolves_to_empty_and_does_not_fan_out(self):
        """When every id in explicit scope no longer corresponds to a
        persisted network, the scope resolves empty. SYNC must NOT
        silently widen back to all_attached — the design choice is to
        send to nobody (operator-resolved decision).

        Scene contains only ``sync`` so the runner exercises the
        scope path directly without going through preset/control
        planning (which has its own coverage in other tests).
        """
        scene_actions = [{"kind": "sync"}]
        _ctrl, runner, t_a, t_b, _, _ = _two_network_fixture(
            scene_actions=scene_actions,
        )
        scene = runner.scenes_service._scenes["test-scene"]  # noqa: SLF001
        scene["network_scope"] = {
            "mode": "explicit",
            "network_ids": ["ghost-net"],
        }

        runner.run("test-scene")
        # Empty scope → no SYNC on any transport.
        self.assertEqual(t_a.sync_calls, [])
        self.assertEqual(t_b.sync_calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
