"""Scene-runner child-action scope inheritance.

A child action inside an ``offset_group`` container that targets
``broadcast`` must fan out only to the PARENT container's scope — not
fleet-wide / not the whole scene scope. "broadcast" on a child means
"the full scope of the parent action".

Pins (runner end-to-end against fake transports):

  * Container targets groups[1] (net_a); a child ``broadcast`` preset
    reaches net_a's transport only — even when the scene's explicit
    network_scope is [net_a, net_b].
  * A ``broadcast`` container keeps the back-compat behaviour: a child
    broadcast reaches every network in the scene scope.
"""

from __future__ import annotations

import unittest

from racelink.domain.models import RL_DeviceGroup, RL_Network
from racelink.services.control_service import ControlService
from racelink.services.gateway_service import GatewayService
from racelink.services.scene_runner_service import SceneRunnerService
from racelink.services.sync_service import SyncService


class _FakeTransport:
    def __init__(self, ident_mac):
        self.ident_mac = ident_mac
        self.preset_calls: list = []
        self.offset_calls: list = []
        self.control_calls: list = []
        self.sync_calls: list = []
        self.listeners: list = []
        self.tx_listeners: list = []

    def send_preset(self, *, recv3, group_id, flags, preset_id, brightness):
        self.preset_calls.append({"recv3": bytes(recv3), "group_id": int(group_id)})

    def send_offset(self, *, recv3, group_id, mode, **kwargs):
        self.offset_calls.append({"group_id": int(group_id), "mode": mode})

    def send_control(self, *, recv3, group_id, flags, **kwargs):
        self.control_calls.append({"recv3": bytes(recv3), "group_id": int(group_id)})

    def send_sync(self, *, recv3, ts24, brightness, flags):
        self.sync_calls.append({"ts24": int(ts24)})

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
    def __init__(self, *, networks, groups, transports):
        self.network_repository = _Repo(networks)
        self.group_repository = _Repo(groups)
        self.device_repository = _Repo([])
        self._transports = list(transports)
        self._transport_hooks_installed_for: set = set()

    @property
    def transports(self):
        return list(self._transports)

    @property
    def transport(self):
        return self._transports[0] if self._transports else None

    def getDeviceFromAddress(self, mac):
        return None

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
        return None

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


def _fixture(scene_actions, *, network_scope=None):
    net_a = RL_Network(name="A", gateway_mac="GW-A")
    net_b = RL_Network(name="B", gateway_mac="GW-B")
    groups = [
        RL_DeviceGroup(name="Unconfigured", static_group=1, dev_type=0, network_id=net_a.id),
        RL_DeviceGroup(name="Team A", static_group=0, dev_type=0, network_id=net_a.id),
        RL_DeviceGroup(name="Team B", static_group=0, dev_type=0, network_id=net_b.id),
    ]
    t_a = _FakeTransport("GW-A")
    t_b = _FakeTransport("GW-B")
    ctrl = _FakeController(networks=[net_a, net_b], groups=groups, transports=[t_a, t_b])
    gw = GatewayService(ctrl)
    ctrl.gateway_service = gw  # type: ignore[attr-defined]
    scene = {"key": "s", "actions": list(scene_actions), "stop_on_error": False}
    if network_scope is not None:
        scene["network_scope"] = network_scope
    runner = SceneRunnerService(
        controller=ctrl,
        scenes_service=_FakeScenesService({"s": scene}),
        control_service=ControlService(controller=ctrl, gateway_service=gw),
        sync_service=SyncService(controller=ctrl, gateway_service=gw),
        sleep=lambda _s: None,
        clock_ms=lambda: 0,
    )
    return ctrl, runner, t_a, t_b, net_a, net_b


class ChildBroadcastScopeTests(unittest.TestCase):

    def test_child_broadcast_limited_to_parent_groups_scope(self):
        """offset_group container targets groups[1] (net_a); the child
        broadcast preset must hit net_a ONLY — even though the scene's
        explicit scope spans both networks."""
        scene_actions = [
            {"kind": "offset_group",
             "target": {"kind": "groups", "value": [1]},  # net_a
             "offset": {"mode": "none"},
             "actions": [
                 {"kind": "wled_preset", "target": {"kind": "broadcast"},
                  "params": {"presetId": 5, "brightness": 64}},
             ]},
        ]
        _ctrl, runner, t_a, t_b, net_a, net_b = _fixture(
            scene_actions,
            network_scope={"mode": "explicit", "network_ids": None},
        )
        # Pin the scene scope to BOTH networks so the only thing that can
        # keep the child broadcast off net_b is the parent-scope inheritance.
        runner.scenes_service._scenes["s"]["network_scope"] = {  # noqa: SLF001
            "mode": "explicit", "network_ids": [net_a.id, net_b.id],
        }

        runner.run("s")

        # Child broadcast preset (group_id 255) reached net_a only.
        bcast_a = [c for c in t_a.preset_calls if c["group_id"] == 255]
        bcast_b = [c for c in t_b.preset_calls if c["group_id"] == 255]
        self.assertEqual(len(bcast_a), 1, "child broadcast must reach net_a")
        self.assertEqual(bcast_b, [], "child broadcast must NOT reach net_b (parent scope = net_a)")

    def test_child_broadcast_under_broadcast_container_reaches_all(self):
        """Back-compat: a broadcast container's child broadcast still fans
        out to every network in the scene scope."""
        scene_actions = [
            {"kind": "offset_group",
             "target": {"kind": "broadcast"},
             "offset": {"mode": "none"},
             "actions": [
                 {"kind": "wled_preset", "target": {"kind": "broadcast"},
                  "params": {"presetId": 5, "brightness": 64}},
             ]},
        ]
        _ctrl, runner, t_a, t_b, net_a, net_b = _fixture(
            scene_actions,
            network_scope={"mode": "explicit", "network_ids": []},
        )
        runner.scenes_service._scenes["s"]["network_scope"] = {  # noqa: SLF001
            "mode": "explicit", "network_ids": [net_a.id, net_b.id],
        }

        runner.run("s")

        bcast_a = [c for c in t_a.preset_calls if c["group_id"] == 255]
        bcast_b = [c for c in t_b.preset_calls if c["group_id"] == 255]
        self.assertEqual(len(bcast_a), 1)
        self.assertEqual(len(bcast_b), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
