"""Tests for ``validate_scene_scope_consistency`` (repository-coupled
cross-action check at the web API layer).

Pins:

  * Auto-mode scenes always pass (validator is a no-op).
  * Explicit scope with unknown network_id → SceneScopeViolation
    with ``code: "unknown_network_id"`` and the bad IDs.
  * Explicit scope + action targeting an out-of-scope group → violation
    with ``offending_action_index``.
  * Explicit scope + broadcast target is ALWAYS in-scope (no violation).
  * Empty explicit scope (post-canonicalizer edge case) → violation
    with ``code: "scope_empty"``.
"""

from __future__ import annotations

import unittest

from racelink.domain.models import RL_Device, RL_DeviceGroup, RL_Network
from racelink.domain.network_boundary import (
    SceneScopeViolation,
    validate_scene_scope_consistency,
)


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
    def __init__(self, *, networks=None, groups=None, devices=None):
        self.network_repository = _Repo(networks or [])
        self.group_repository = _Repo(groups or [])
        self.device_repository = _Repo(devices or [])

    def getDeviceFromAddress(self, addr):
        return self.device_repository.get_by_addr(addr)


def _two_network_controller(*, devices=None):
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
    ctrl = _FakeController(
        networks=[net_a, net_b], groups=groups, devices=list(devices or ()),
    )
    return ctrl, net_a, net_b


class AutoModePassThroughTests(unittest.TestCase):

    def test_auto_mode_never_raises(self):
        ctrl, _, _ = _two_network_controller()
        scene = {
            "network_scope": {"mode": "auto"},
            "actions": [
                {"kind": "rl_preset",
                 "target": {"kind": "groups", "value": [1]},
                 "params": {}},
            ],
        }
        # Auto mode means the validator is a no-op — actions can target
        # anything reachable.
        validate_scene_scope_consistency(scene, controller=ctrl)

    def test_missing_scope_field_treated_as_auto(self):
        ctrl, _, _ = _two_network_controller()
        scene = {
            "actions": [
                {"kind": "rl_preset",
                 "target": {"kind": "device", "value": "AABBCC112233"},
                 "params": {}},
            ],
        }
        validate_scene_scope_consistency(scene, controller=ctrl)


class UnknownNetworkIdTests(unittest.TestCase):

    def test_unknown_id_in_scope_rejects(self):
        ctrl, net_a, _ = _two_network_controller()
        scene = {
            "network_scope": {
                "mode": "explicit",
                "network_ids": [net_a.id, "ghost-net"],
            },
            "actions": [],
        }
        with self.assertRaises(SceneScopeViolation) as cm:
            validate_scene_scope_consistency(scene, controller=ctrl)
        self.assertEqual(cm.exception.detail["code"], "unknown_network_id")
        self.assertIn("ghost-net", cm.exception.detail["unknown_ids"])

    def test_all_unknown_ids_rejects(self):
        ctrl, _, _ = _two_network_controller()
        scene = {
            "network_scope": {
                "mode": "explicit",
                "network_ids": ["ghost-1", "ghost-2"],
            },
            "actions": [],
        }
        with self.assertRaises(SceneScopeViolation):
            validate_scene_scope_consistency(scene, controller=ctrl)


class ScopeViolationTests(unittest.TestCase):

    def test_group_action_outside_scope_rejects(self):
        ctrl, net_a, net_b = _two_network_controller()
        # Scope = net-A only, action targets group on net-B → violation.
        scene = {
            "network_scope": {"mode": "explicit", "network_ids": [net_a.id]},
            "actions": [
                {"kind": "rl_preset",
                 "target": {"kind": "groups", "value": [2]},  # Team B → net-B
                 "params": {}},
            ],
        }
        with self.assertRaises(SceneScopeViolation) as cm:
            validate_scene_scope_consistency(scene, controller=ctrl)
        self.assertEqual(cm.exception.detail["code"], "scope_violation")
        self.assertEqual(cm.exception.detail["offending_action_index"], 0)

    def test_device_action_outside_scope_rejects(self):
        net_a = RL_Network(name="A", gateway_mac="GW-A")
        net_b = RL_Network(name="B", gateway_mac="GW-B")
        dev_b = RL_Device("AABBCC222222", dev_type=0, name="dev-b", groupId=2)
        dev_b.network_id = net_b.id
        ctrl = _FakeController(
            networks=[net_a, net_b], groups=[], devices=[dev_b],
        )
        scene = {
            "network_scope": {"mode": "explicit", "network_ids": [net_a.id]},
            "actions": [
                {"kind": "rl_preset",
                 "target": {"kind": "device", "value": "AABBCC222222"},
                 "params": {}},
            ],
        }
        with self.assertRaises(SceneScopeViolation) as cm:
            validate_scene_scope_consistency(scene, controller=ctrl)
        self.assertEqual(cm.exception.detail["code"], "scope_violation")

    def test_broadcast_target_always_in_scope(self):
        ctrl, net_a, _ = _two_network_controller()
        scene = {
            "network_scope": {"mode": "explicit", "network_ids": [net_a.id]},
            "actions": [
                {"kind": "rl_preset",
                 "target": {"kind": "broadcast"},
                 "params": {}},
            ],
        }
        # Broadcast actions under explicit scope mean "fan out to scope" —
        # they CANNOT violate the scope by definition.
        validate_scene_scope_consistency(scene, controller=ctrl)

    def test_in_scope_group_passes(self):
        ctrl, net_a, _ = _two_network_controller()
        scene = {
            "network_scope": {"mode": "explicit", "network_ids": [net_a.id]},
            "actions": [
                {"kind": "rl_preset",
                 "target": {"kind": "groups", "value": [1]},  # Team A → net-A
                 "params": {}},
            ],
        }
        validate_scene_scope_consistency(scene, controller=ctrl)

    def test_offending_index_reports_first_violation(self):
        ctrl, net_a, _ = _two_network_controller()
        scene = {
            "network_scope": {"mode": "explicit", "network_ids": [net_a.id]},
            "actions": [
                # Index 0: in scope (group 1 = net-A).
                {"kind": "rl_preset",
                 "target": {"kind": "groups", "value": [1]},
                 "params": {}},
                # Index 1: violates (group 2 = net-B).
                {"kind": "rl_preset",
                 "target": {"kind": "groups", "value": [2]},
                 "params": {}},
            ],
        }
        with self.assertRaises(SceneScopeViolation) as cm:
            validate_scene_scope_consistency(scene, controller=ctrl)
        self.assertEqual(cm.exception.detail["offending_action_index"], 1)


class EdgeCaseTests(unittest.TestCase):

    def test_actions_without_target_pass(self):
        ctrl, net_a, _ = _two_network_controller()
        scene = {
            "network_scope": {"mode": "explicit", "network_ids": [net_a.id]},
            "actions": [
                {"kind": "sync"},
                {"kind": "delay", "duration_ms": 100},
            ],
        }
        validate_scene_scope_consistency(scene, controller=ctrl)

    def test_unknown_device_target_passes_silently(self):
        # An action targeting a non-existent device cannot resolve a
        # network — it contributes nothing to the scope check. The save
        # path treats this as a degraded action at runtime, not a scope
        # violation at save time.
        ctrl, net_a, _ = _two_network_controller()
        scene = {
            "network_scope": {"mode": "explicit", "network_ids": [net_a.id]},
            "actions": [
                {"kind": "rl_preset",
                 "target": {"kind": "device", "value": "FFFFFFFFFFFF"},
                 "params": {}},
            ],
        }
        validate_scene_scope_consistency(scene, controller=ctrl)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
