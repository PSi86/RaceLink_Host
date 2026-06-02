"""Unit tests for :func:`scene_network_ids`.

Pins:

  * ``target.kind == "broadcast"`` resolves to every persisted
    network (group 255 is fleet-wide by design).
  * ``target.kind == "groups"`` resolves each gid → group.network_id.
    Unknown gids are skipped silently.
  * ``target.kind == "device"`` resolves device.addr → network_id.
    Unknown devices are skipped silently.
  * Actions without a target (sync, delay) contribute nothing — they
    inherit the union from the rest of the scene.
  * offset_group containers descend into children.
  * Result is deduplicated and ordered by first occurrence.
"""

from __future__ import annotations

import unittest

from racelink.domain.models import RL_Device, RL_DeviceGroup, RL_Network
from racelink.services.scene_network_scope import scene_network_ids


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


class _FakeController:
    def __init__(self, *, networks, groups, devices):
        self.network_repository = _Repo(networks)
        self.group_repository = _Repo(groups)
        self.device_repository = _Repo(devices)

    def getDeviceFromAddress(self, addr):
        return self.device_repository.get_by_addr(addr)


def _device(addr, *, network_id, group_id=0):
    d = RL_Device(addr, dev_type=0, name=addr, groupId=group_id)
    d.network_id = network_id
    return d


def _ctrl_two_networks(*, devices=None, extra_groups=None):
    """Two-network fixture used by most cases below. Networks A + B,
    a Unconfigured group on each + a Team group on each.

    Group repository layout (index = group_id):
        0 = Unconfigured / net-A
        1 = Team A      / net-A
        2 = Team B      / net-B
    """
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
    if extra_groups:
        groups.extend(extra_groups)
    return _FakeController(
        networks=[net_a, net_b],
        groups=groups,
        devices=list(devices or ()),
    ), net_a, net_b


class TargetResolutionTests(unittest.TestCase):

    def test_broadcast_target_collects_every_network(self):
        ctrl, net_a, net_b = _ctrl_two_networks()
        scene = {"actions": [
            {"kind": "rl_preset", "target": {"kind": "broadcast"}, "params": {}},
        ]}
        ids = scene_network_ids(scene, controller=ctrl)
        self.assertEqual(set(ids), {net_a.id, net_b.id})

    def test_groups_target_resolves_each_gid(self):
        ctrl, net_a, net_b = _ctrl_two_networks()
        scene = {"actions": [
            {"kind": "rl_preset", "target": {"kind": "groups", "value": [1]}, "params": {}},
            {"kind": "rl_preset", "target": {"kind": "groups", "value": [2]}, "params": {}},
        ]}
        ids = scene_network_ids(scene, controller=ctrl)
        self.assertEqual(set(ids), {net_a.id, net_b.id})

    def test_groups_target_skips_unknown_gids(self):
        ctrl, net_a, _ = _ctrl_two_networks()
        scene = {"actions": [
            {"kind": "rl_preset", "target": {"kind": "groups", "value": [1, 99]}, "params": {}},
        ]}
        ids = scene_network_ids(scene, controller=ctrl)
        # gid 99 doesn't exist; only gid 1's network (A) makes it in.
        self.assertEqual(tuple(ids), (net_a.id,))

    def test_device_target_resolves_to_devices_network(self):
        dev_b = _device("AABBCC222222", network_id="placeholder", group_id=2)
        ctrl, net_a, net_b = _ctrl_two_networks(devices=[dev_b])
        # Re-bind the device to net_b's *real* id (the placeholder is
        # overwritten because RL_Network.id is generated per instance).
        dev_b.network_id = net_b.id

        scene = {"actions": [
            {"kind": "rl_preset", "target": {"kind": "device", "value": "AABBCC222222"}, "params": {}},
        ]}
        ids = scene_network_ids(scene, controller=ctrl)
        self.assertEqual(tuple(ids), (net_b.id,))

    def test_device_target_skips_unknown_addr(self):
        ctrl, _, _ = _ctrl_two_networks()
        scene = {"actions": [
            {"kind": "rl_preset", "target": {"kind": "device", "value": "AABBCC999999"}, "params": {}},
        ]}
        ids = scene_network_ids(scene, controller=ctrl)
        self.assertEqual(ids, ())


class ActionShapeTests(unittest.TestCase):

    def test_sync_and_delay_contribute_nothing_alone(self):
        ctrl, _, _ = _ctrl_two_networks()
        scene = {"actions": [
            {"kind": "sync"},
            {"kind": "delay", "duration_ms": 100},
        ]}
        ids = scene_network_ids(scene, controller=ctrl)
        # Nothing has a target → empty scope.
        self.assertEqual(ids, ())

    def test_mixed_scene_unions_targets(self):
        ctrl, net_a, net_b = _ctrl_two_networks()
        scene = {"actions": [
            {"kind": "rl_preset", "target": {"kind": "groups", "value": [1]}, "params": {}},
            {"kind": "sync"},
            {"kind": "delay", "duration_ms": 50},
            {"kind": "rl_preset", "target": {"kind": "groups", "value": [2]}, "params": {}},
        ]}
        ids = scene_network_ids(scene, controller=ctrl)
        self.assertEqual(set(ids), {net_a.id, net_b.id})

    def test_offset_group_descends_into_children(self):
        ctrl, net_a, net_b = _ctrl_two_networks()
        scene = {"actions": [
            {"kind": "offset_group",
             "target": {"kind": "broadcast"},  # also contributes
             "offset": {"mode": "none"},
             "actions": [
                 {"kind": "rl_preset", "target": {"kind": "groups", "value": [1]}, "params": {}},
                 {"kind": "rl_preset", "target": {"kind": "groups", "value": [2]}, "params": {}},
             ]},
        ]}
        ids = scene_network_ids(scene, controller=ctrl)
        # broadcast contributes net_a + net_b; children also do.
        # Deduplicated; ordering: container's target first, then children.
        self.assertEqual(set(ids), {net_a.id, net_b.id})

    def test_child_broadcast_inherits_parent_groups_scope(self):
        """A child action's ``broadcast`` under a non-broadcast container
        means "the parent's scope", NOT fleet-wide. Container targets
        groups[1] (net_a); the child broadcast must resolve to net_a only,
        never net_b."""
        ctrl, net_a, net_b = _ctrl_two_networks()
        scene = {"actions": [
            {"kind": "offset_group",
             "target": {"kind": "groups", "value": [1]},  # net_a only
             "offset": {"mode": "none"},
             "actions": [
                 {"kind": "wled_preset", "target": {"kind": "broadcast"}, "params": {}},
             ]},
        ]}
        ids = scene_network_ids(scene, controller=ctrl)
        self.assertEqual(tuple(ids), (net_a.id,))
        self.assertNotIn(net_b.id, ids)

    def test_child_broadcast_under_broadcast_parent_stays_fleet_wide(self):
        """When the container itself is ``broadcast``, a child broadcast
        inherits that full (fleet-wide) scope — back-compat."""
        ctrl, net_a, net_b = _ctrl_two_networks()
        scene = {"actions": [
            {"kind": "offset_group",
             "target": {"kind": "broadcast"},
             "offset": {"mode": "none"},
             "actions": [
                 {"kind": "wled_preset", "target": {"kind": "broadcast"}, "params": {}},
             ]},
        ]}
        ids = scene_network_ids(scene, controller=ctrl)
        self.assertEqual(set(ids), {net_a.id, net_b.id})

    def test_ordering_is_first_occurrence(self):
        ctrl, net_a, net_b = _ctrl_two_networks()
        scene = {"actions": [
            {"kind": "rl_preset", "target": {"kind": "groups", "value": [2]}, "params": {}},
            {"kind": "rl_preset", "target": {"kind": "groups", "value": [1]}, "params": {}},
            {"kind": "rl_preset", "target": {"kind": "groups", "value": [2]}, "params": {}},
        ]}
        ids = scene_network_ids(scene, controller=ctrl)
        # First action targets group 2 (net_b), then group 1 (net_a),
        # then group 2 again (already in set). Order: net_b, net_a.
        self.assertEqual(ids, (net_b.id, net_a.id))


class EdgeCaseTests(unittest.TestCase):

    def test_missing_repositories_return_empty(self):
        class _StubCtrl:
            pass
        scene = {"actions": [
            {"kind": "rl_preset", "target": {"kind": "broadcast"}, "params": {}},
        ]}
        self.assertEqual(
            scene_network_ids(scene, controller=_StubCtrl()),
            (),
        )

    def test_scene_without_actions_returns_empty(self):
        ctrl, _, _ = _ctrl_two_networks()
        self.assertEqual(scene_network_ids({}, controller=ctrl), ())
        self.assertEqual(scene_network_ids({"actions": []}, controller=ctrl), ())

    def test_non_dict_scene_returns_empty(self):
        ctrl, _, _ = _ctrl_two_networks()
        self.assertEqual(scene_network_ids(None, controller=ctrl), ())
        self.assertEqual(scene_network_ids([], controller=ctrl), ())


class ExplicitScopeTests(unittest.TestCase):
    """Explicit scope wins over the action walk (operator override)."""

    def test_explicit_scope_wins_over_actions(self):
        ctrl, net_a, net_b = _ctrl_two_networks()
        scene = {
            "network_scope": {"mode": "explicit", "network_ids": [net_a.id]},
            "actions": [
                # Action targets net-B's group, but the explicit scope
                # locks the runtime view to net-A.
                {"kind": "rl_preset", "target": {"kind": "groups", "value": [2]},
                 "params": {}},
            ],
        }
        ids = scene_network_ids(scene, controller=ctrl)
        self.assertEqual(ids, (net_a.id,))

    def test_explicit_scope_with_stale_id_drops_silently(self):
        ctrl, net_a, _ = _ctrl_two_networks()
        scene = {
            "network_scope": {
                "mode": "explicit",
                "network_ids": [net_a.id, "ghost-net"],
            },
            "actions": [],
        }
        ids = scene_network_ids(scene, controller=ctrl)
        # Only the persisted-known ID survives.
        self.assertEqual(ids, (net_a.id,))

    def test_explicit_scope_all_stale_returns_empty(self):
        ctrl, _, _ = _ctrl_two_networks()
        scene = {
            "network_scope": {
                "mode": "explicit",
                "network_ids": ["ghost-1", "ghost-2"],
            },
            "actions": [
                # Action would resolve to a real network, but explicit
                # scope's filter wins: no live ids → empty tuple. The
                # runner treats this as a degraded run.
                {"kind": "rl_preset", "target": {"kind": "broadcast"},
                 "params": {}},
            ],
        }
        ids = scene_network_ids(scene, controller=ctrl)
        self.assertEqual(ids, ())

    def test_explicit_scope_with_no_repo_keeps_ids_verbatim(self):
        # When the controller has no network repo (test fakes,
        # standalone harness), the soft-filter cannot validate — pass
        # through.
        class _StubCtrl:
            pass
        scene = {
            "network_scope": {
                "mode": "explicit",
                "network_ids": ["net-x", "net-y"],
            },
            "actions": [],
        }
        ids = scene_network_ids(scene, controller=_StubCtrl())
        self.assertEqual(ids, ("net-x", "net-y"))

    def test_explicit_scope_preserves_order(self):
        ctrl, net_a, net_b = _ctrl_two_networks()
        scene = {
            "network_scope": {
                "mode": "explicit",
                "network_ids": [net_b.id, net_a.id],
            },
            "actions": [],
        }
        ids = scene_network_ids(scene, controller=ctrl)
        self.assertEqual(ids, (net_b.id, net_a.id))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
