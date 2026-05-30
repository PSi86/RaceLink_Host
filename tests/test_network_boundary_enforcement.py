"""Network-boundary enforcement tests (Stage 3 Part B).

Pins three layers:

  1. Pure validator (:func:`validate_group_membership`):
     - same-network device + same-network group → no raise
     - target_group_id == 0 (Unconfigured) → never raises
     - devices on multiple networks → ``devices_span_multiple_networks``
     - devices on net X, group on net Y → ``group_network_mismatch``
     - missing ``network_id`` on devices / groups: skip silently
       (back-compat for unmigrated payloads)

  2. Group creation routes
     (``api_groups_create``, ``_prepare_discover_target``) stamp the
     default network's id on every fresh group.

  3. ``/api/devices/update-meta`` returns HTTP 400 with the
     structured ``detail`` shape on a boundary violation, BEFORE
     the TaskManager job is started.
"""

from __future__ import annotations

import unittest
from unittest import mock

from racelink.domain import network_boundary
from racelink.domain.models import RL_Device, RL_DeviceGroup, RL_Network
from racelink.domain.network_boundary import (
    NetworkBoundaryViolation,
    validate_group_membership,
    validate_network_kind_match,
)


def _dev(mac, *, network_id=None):
    d = RL_Device(addr=mac, dev_type=0, name=mac)
    if network_id is not None:
        d.network_id = network_id
    return d


def _grp(name, *, network_id=None):
    return RL_DeviceGroup(name=name, static_group=0, dev_type=0,
                          network_id=network_id)


class ValidateGroupMembershipTests(unittest.TestCase):

    def test_same_network_devices_and_group_pass(self):
        devs = [_dev("AABBCC112233", network_id="net-a"),
                _dev("AABBCC112244", network_id="net-a")]
        grp = _grp("Team A", network_id="net-a")
        # Pure function — should not raise.
        validate_group_membership(devs, grp, target_group_id=3)

    def test_unconfigured_target_short_circuits(self):
        # Even if devices span networks AND target group is unrelated,
        # moving them into Unconfigured (id 0) is always allowed —
        # it's the "remove from group" sink.
        devs = [_dev("AA", network_id="net-a"),
                _dev("BB", network_id="net-b")]
        grp = _grp("Unconfigured", network_id="net-x")
        validate_group_membership(devs, grp, target_group_id=0)

    def test_devices_span_multiple_networks_raises(self):
        devs = [_dev("AA", network_id="net-a"),
                _dev("BB", network_id="net-b")]
        grp = _grp("Team A", network_id="net-a")
        with self.assertRaises(NetworkBoundaryViolation) as ctx:
            validate_group_membership(devs, grp, target_group_id=3)
        self.assertEqual(ctx.exception.detail["code"],
                         "devices_span_multiple_networks")
        self.assertEqual(set(ctx.exception.detail["networks"].keys()),
                         {"net-a", "net-b"})

    def test_group_network_mismatch_raises(self):
        devs = [_dev("AA", network_id="net-a"),
                _dev("BB", network_id="net-a")]
        grp = _grp("Team B", network_id="net-b")
        with self.assertRaises(NetworkBoundaryViolation) as ctx:
            validate_group_membership(devs, grp, target_group_id=4)
        detail = ctx.exception.detail
        self.assertEqual(detail["code"], "group_network_mismatch")
        self.assertEqual(detail["device_network_id"], "net-a")
        self.assertEqual(detail["group_network_id"], "net-b")
        self.assertEqual(set(detail["device_macs"]), {"AA", "BB"})
        self.assertEqual(detail["group_id"], 4)

    def test_devices_without_network_id_are_ignored(self):
        # Legacy payload: device record has no network_id yet
        # (persistence-migration hasn't run, or test fake). Single
        # such device cannot conflict with anything.
        devs = [_dev("AA")]
        grp = _grp("Team A", network_id="net-a")
        # No exception — validator skips the check entirely.
        validate_group_membership(devs, grp, target_group_id=3)

    def test_group_without_network_id_is_ignored(self):
        # Mirror of the previous case: a legacy group (pre-migration)
        # has no network_id constraint, so move is allowed.
        devs = [_dev("AA", network_id="net-a")]
        grp = _grp("Team A")  # no network_id
        validate_group_membership(devs, grp, target_group_id=3)

    def test_empty_devices_list_is_noop(self):
        grp = _grp("Team A", network_id="net-a")
        validate_group_membership([], grp, target_group_id=3)

    def test_target_group_none_is_tolerated(self):
        # The route hands ``None`` when the target_group_id was
        # out-of-range. The validator treats that as "no group
        # network constraint to enforce".
        devs = [_dev("AA", network_id="net-a")]
        validate_group_membership(devs, None, target_group_id=-1)

    def test_violation_reason_has_operator_readable_text(self):
        devs = [_dev("AA", network_id="net-a"),
                _dev("BB", network_id="net-a")]
        grp = _grp("Team B", network_id="net-b")
        with self.assertRaises(NetworkBoundaryViolation) as ctx:
            validate_group_membership(devs, grp, target_group_id=4)
        msg = str(ctx.exception)
        # Mentions the group name + the recovery path (RF migration).
        self.assertIn("Team B", msg)
        self.assertIn("migration", msg.lower())


class ValidateNetworkKindMatchTests(unittest.TestCase):
    """Block D: forbid migrating a group/device across network *kinds*."""

    def test_same_kind_passes(self):
        target = RL_Network(name="RF B", kind="rf")
        sources = [RL_Network(name="RF A", kind="rf"),
                   RL_Network(name="RF A2", kind="rf")]
        validate_network_kind_match(target, sources)  # no raise

    def test_ethernet_to_ethernet_passes(self):
        target = RL_Network(name="LAN B", kind="ethernet")
        sources = [RL_Network(name="LAN A", kind="ethernet")]
        validate_network_kind_match(target, sources)  # no raise

    def test_rf_to_ethernet_raises(self):
        target = RL_Network(name="LAN", kind="ethernet")
        source = RL_Network(name="Track A", kind="rf")
        with self.assertRaises(NetworkBoundaryViolation) as ctx:
            validate_network_kind_match(target, [source])
        self.assertEqual(ctx.exception.detail["code"], "network_kind_mismatch")
        self.assertEqual(ctx.exception.detail["target_kind"], "ethernet")
        self.assertEqual(
            ctx.exception.detail["source_networks"][0]["kind"], "rf",
        )

    def test_ethernet_to_rf_raises(self):
        target = RL_Network(name="Track A", kind="rf")
        source = RL_Network(name="LAN", kind="ethernet")
        with self.assertRaises(NetworkBoundaryViolation) as ctx:
            validate_network_kind_match(target, [source])
        self.assertEqual(ctx.exception.detail["target_kind"], "rf")

    def test_none_sources_are_ignored(self):
        target = RL_Network(name="RF B", kind="rf")
        # Unbound / legacy entries (None) never conflict.
        validate_network_kind_match(target, [None, None])  # no raise

    def test_mixed_sources_reports_only_mismatch(self):
        target = RL_Network(name="RF B", kind="rf")
        rf_src = RL_Network(name="RF A", kind="rf")
        eth_src = RL_Network(name="LAN", kind="ethernet")
        with self.assertRaises(NetworkBoundaryViolation) as ctx:
            validate_network_kind_match(target, [rf_src, eth_src])
        mism = ctx.exception.detail["source_networks"]
        self.assertEqual(len(mism), 1)
        self.assertEqual(mism[0]["kind"], "ethernet")

    def test_legacy_network_without_kind_treated_as_rf(self):
        # A dict source missing 'kind' compares as the historical RF kind.
        target = RL_Network(name="RF B", kind="rf")
        validate_network_kind_match(target, [{"id": "legacy", "name": "Old"}])


class GroupCreateDefaultNetworkBindingTests(unittest.TestCase):
    """When the operator creates a fresh group via the API (and
    in the discover-time created-group helper), the new group
    inherits the default network's id so the validator has a
    concrete anchor on every future bulk-set call."""

    def _make_ctx(self, default_network_id=None):
        # Light-weight stand-in for the ``RouteContext`` shape that
        # ``api.py`` builds at blueprint registration. The two
        # group-create call sites only read a handful of fields:
        # ``rl_lock``, ``rl_instance.network_repository``,
        # ``RL_DeviceGroup``, ``group_repo``, ``groups()``,
        # ``log``, ``rl_grouplist``.
        from threading import RLock

        class _NetRepo:
            def __init__(self, nets):
                self._nets = nets

            def list(self):
                return list(self._nets)

        class _RL:
            def __init__(self, default_id):
                self.network_repository = _NetRepo(
                    [RL_Network(id=default_id, name="Default")]
                    if default_id is not None else []
                )

        class _GroupRepo:
            def __init__(self):
                self.items: list = []

            def append(self, group):
                self.items.append(group)
                return len(self.items) - 1

        class _Ctx:
            pass

        ctx = _Ctx()
        ctx.rl_lock = RLock()
        ctx.rl_instance = _RL(default_network_id)
        ctx.RL_DeviceGroup = RL_DeviceGroup
        ctx.group_repo = _GroupRepo()
        ctx.rl_grouplist = ctx.group_repo.items  # alias
        ctx.log = lambda *_a, **_kw: None
        return ctx

    def test_prepare_discover_target_stamps_default_network(self):
        from racelink.web.api import _prepare_discover_target

        default_id = "00000000-0000-4000-8000-000000000001"
        ctx = self._make_ctx(default_network_id=default_id)
        target_gid, created_gid = _prepare_discover_target(
            ctx, target_gid=None, new_group_name="Pit-Lane",
        )
        self.assertIsNotNone(created_gid)
        created_group = ctx.group_repo.items[created_gid]
        self.assertEqual(created_group.network_id, default_id)
        self.assertEqual(created_group.name, "Pit-Lane")

    def test_prepare_discover_target_no_networks_yet_leaves_none(self):
        from racelink.web.api import _prepare_discover_target

        ctx = self._make_ctx(default_network_id=None)
        _, created_gid = _prepare_discover_target(
            ctx, target_gid=None, new_group_name="Pit-Lane",
        )
        # The route still creates the group, but ``network_id``
        # stays ``None`` — the boundary validator will skip the
        # check for it (back-compat).
        self.assertIsNotNone(created_gid)
        created_group = ctx.group_repo.items[created_gid]
        self.assertIsNone(created_group.network_id)


class IntegrationCheckTests(unittest.TestCase):
    """The validator must reject before the TaskManager runs — the
    HTTP 400 contract pins this so the WebUI's toast has the
    structured ``detail`` payload to render."""

    def test_validator_raises_carry_detail(self):
        # Exercise the exception type directly so the integration
        # contract is documented even without spinning up the full
        # Flask stack here. Integration-tests covering the wired
        # route live in ``tests/test_api_routes.py`` (existing
        # suite already exercises ``/api/devices/update-meta``).
        ex = NetworkBoundaryViolation(
            "reason text",
            detail={"code": "group_network_mismatch", "group_id": 7},
        )
        self.assertEqual(ex.reason, "reason text")
        self.assertEqual(ex.detail["code"], "group_network_mismatch")
        self.assertEqual(str(ex), "reason text")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
