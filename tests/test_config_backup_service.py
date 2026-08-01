"""Versioned config snapshots (:mod:`racelink.services.config_backup_service`).

Pins the contracts that make "start a fresh configuration" reversible:

  * A snapshot records the schema version *and* the host version that
    wrote it, plus record counts for the picker.
  * Restore never destroys the outgoing configuration — it takes a
    ``pre-restore`` snapshot first.
  * Restore refuses a payload from a newer host unless forced, because
    this build would silently drop fields it does not understand on the
    next save.
  * Restore/clear route the payload through the controller's normal
    ``load_from_db`` so schema migration stays in one place.
  * Clear emits a **v1** payload on purpose, so the v1→v2 migration
    synthesises the default network and the result equals a fresh
    install rather than a networkless config.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from racelink._version import VERSION as HOST_VERSION
from racelink.services.config_backup_service import (
    ConfigBackupError,
    ConfigBackupService,
)
from racelink.state.persistence import CURRENT_SCHEMA_VERSION


class _Rec:
    """Minimal record — ``dump_state`` serialises ``__dict__``."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Repo:
    def __init__(self, items=()):
        self._items = list(items)

    def list(self):
        return list(self._items)


class _FakeController:
    def __init__(self, *, devices=(), groups=(), networks=()):
        self.device_repository = _Repo(devices)
        self.group_repository = _Repo(groups)
        self.network_repository = _Repo(networks)
        self.options: dict[str, str] = {}
        self.load_calls = 0
        self.save_calls: list[dict] = []

    def _option_set(self, key, value):
        self.options[key] = value

    def _option(self, key, default=None):
        return self.options.get(key, default)

    def load_from_db(self):
        self.load_calls += 1

    def save_to_db(self, args, scopes=None):
        self.save_calls.append({"args": args, "scopes": scopes})


class _BackupTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch(
            "racelink.services.config_backup_service.user_data_path",
            side_effect=lambda *parts: os.path.join(self._tmp.name, *parts),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _service(self, **kw):
        ctrl = _FakeController(**kw)
        return ctrl, ConfigBackupService(ctrl)


class CreateAndListTests(_BackupTestCase):

    def test_create_records_versions_and_counts(self):
        ctrl, svc = self._service(
            devices=[_Rec(addr="AA"), _Rec(addr="BB")],
            groups=[_Rec(groupId=1)],
            networks=[_Rec(id="net-1", name="Start")],
        )

        meta = svc.create(label="before the swap")

        self.assertEqual(meta["host_version"], HOST_VERSION)
        self.assertEqual(meta["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(meta["counts"],
                         {"devices": 2, "groups": 1, "networks": 1})
        self.assertEqual(meta["label"], "before the swap")
        self.assertEqual(meta["reason"], "manual")
        self.assertTrue(meta["compatible"])
        # Listings must not carry the payload — the picker only needs meta.
        self.assertNotIn("state", meta)

        stored = svc.read(meta["id"])
        payload = json.loads(stored["state"])
        self.assertEqual(len(payload["devices"]), 2)
        self.assertEqual(payload["schema_version"], CURRENT_SCHEMA_VERSION)

    def test_same_second_creates_do_not_overwrite(self):
        """Two clicks a moment apart must not leave one snapshot. The id
        is second-granular, so this collides by construction."""
        _ctrl, svc = self._service(devices=[_Rec(addr="AA")])

        a = svc.create(label="one")
        b = svc.create(label="two")

        self.assertNotEqual(a["id"], b["id"])
        labels = sorted(m["label"] for m in svc.list())
        self.assertEqual(labels, ["one", "two"])

    def test_list_is_newest_first(self):
        _ctrl, svc = self._service()
        first = svc.create(label="older")
        second = svc.create(label="newer")
        # Force a deterministic ordering regardless of clock resolution.
        path = os.path.join(svc.backup_dir(), f"{second['id']}.json")
        with open(path, "r", encoding="utf-8") as fh:
            env = json.load(fh)
        env["created_ts"] = float(first["created_ts"]) + 100.0
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(env, fh)

        rows = svc.list()

        self.assertEqual([r["id"] for r in rows], [second["id"], first["id"]])

    def test_unreadable_backup_is_listed_with_an_error(self):
        """A corrupt snapshot the operator still believes in is worse
        than one that visibly failed."""
        _ctrl, svc = self._service()
        with open(os.path.join(svc.backup_dir(), "state_broken.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{not json")

        rows = svc.list()

        broken = [r for r in rows if r["id"] == "state_broken"]
        self.assertEqual(len(broken), 1)
        self.assertIn("unreadable", broken[0]["error"])
        self.assertFalse(broken[0]["compatible"])

    def test_rejects_traversing_ids(self):
        _ctrl, svc = self._service()
        for bad in ("../evil", "a/b", "..", "with space"):
            with self.assertRaises(ConfigBackupError):
                svc.read(bad)


class RestoreTests(_BackupTestCase):

    def test_restore_swaps_state_through_the_normal_load_path(self):
        ctrl, svc = self._service(devices=[_Rec(addr="AA")])
        meta = svc.create(label="snapshot")
        stored_state = svc.read(meta["id"])["state"]

        out = svc.restore(meta["id"])

        self.assertTrue(out["ok"])
        self.assertEqual(ctrl.options["rl_state_v1"], stored_state)
        # Migration/legacy handling must not be duplicated here.
        self.assertEqual(ctrl.load_calls, 1)
        self.assertEqual(len(ctrl.save_calls), 1)

    def test_restore_snapshots_the_outgoing_config_first(self):
        _ctrl, svc = self._service(devices=[_Rec(addr="AA")])
        meta = svc.create(label="target")

        out = svc.restore(meta["id"])

        pre = out["pre_restore_backup"]
        self.assertEqual(pre["reason"], "pre-restore")
        self.assertIn(meta["id"], pre["label"])
        self.assertIn(pre["id"], [r["id"] for r in svc.list()])

    def test_refuses_newer_schema_without_force(self):
        _ctrl, svc = self._service()
        meta = svc.create()
        path = os.path.join(svc.backup_dir(), f"{meta['id']}.json")
        with open(path, "r", encoding="utf-8") as fh:
            env = json.load(fh)
        env["schema_version"] = CURRENT_SCHEMA_VERSION + 1
        env["host_version"] = "9.9.9"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(env, fh)

        self.assertFalse(svc.list()[0]["compatible"])
        with self.assertRaises(ConfigBackupError) as ctx:
            svc.restore(meta["id"])
        self.assertIn("newer host", str(ctx.exception))

    def test_force_accepts_newer_schema(self):
        ctrl, svc = self._service()
        meta = svc.create()
        path = os.path.join(svc.backup_dir(), f"{meta['id']}.json")
        with open(path, "r", encoding="utf-8") as fh:
            env = json.load(fh)
        env["schema_version"] = CURRENT_SCHEMA_VERSION + 1
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(env, fh)

        out = svc.restore(meta["id"], force=True)

        self.assertTrue(out["ok"])
        self.assertEqual(ctrl.load_calls, 1)

    def test_older_schema_restores_without_force(self):
        """The common case: the migration chain fills in what was added
        since, so an older snapshot needs no ceremony."""
        ctrl, svc = self._service()
        meta = svc.create()
        path = os.path.join(svc.backup_dir(), f"{meta['id']}.json")
        with open(path, "r", encoding="utf-8") as fh:
            env = json.load(fh)
        env["schema_version"] = 1
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(env, fh)

        self.assertTrue(svc.list()[0]["compatible"])
        out = svc.restore(meta["id"])

        self.assertTrue(out["ok"])
        self.assertEqual(ctrl.load_calls, 1)

    def test_unknown_and_payloadless_backups_error(self):
        _ctrl, svc = self._service()
        with self.assertRaises(ConfigBackupError):
            svc.restore("state_nope")

        meta = svc.create()
        path = os.path.join(svc.backup_dir(), f"{meta['id']}.json")
        with open(path, "r", encoding="utf-8") as fh:
            env = json.load(fh)
        env["state"] = ""
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(env, fh)
        with self.assertRaises(ConfigBackupError) as ctx:
            svc.restore(meta["id"])
        self.assertIn("no state payload", str(ctx.exception))


class ClearTests(_BackupTestCase):

    def test_clear_backs_up_then_writes_an_empty_v1_payload(self):
        ctrl, svc = self._service(
            devices=[_Rec(addr="AA")],
            networks=[_Rec(id="net-1", name="Start")],
        )

        out = svc.clear(label="fresh start")

        self.assertTrue(out["ok"])
        self.assertEqual(out["backup"]["reason"], "pre-clear")
        self.assertEqual(out["backup"]["counts"]["devices"], 1)

        payload = json.loads(ctrl.options["rl_state_v1"])
        self.assertEqual(payload["devices"], [])
        self.assertEqual(payload["groups"], [])
        self.assertEqual(payload["networks"], [])
        # v1 on purpose: the v1->v2 step synthesises the default network,
        # so the reload lands on a fresh-install state, not a config with
        # zero networks that no other path ever produces.
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(ctrl.load_calls, 1)

    def test_clear_uses_a_default_label_when_none_given(self):
        _ctrl, svc = self._service()

        out = svc.clear()

        self.assertTrue(out["backup"]["label"])


class DeleteTests(_BackupTestCase):

    def test_delete_removes_the_snapshot(self):
        _ctrl, svc = self._service()
        meta = svc.create()

        svc.delete(meta["id"])

        self.assertEqual(svc.list(), [])
        with self.assertRaises(ConfigBackupError):
            svc.delete(meta["id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
