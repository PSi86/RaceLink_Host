"""Tests for SceneService — CRUD, schema validation, on_changed hook."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from racelink.services.scenes_service import (
    KIND_DELAY,
    KIND_RL_PRESET,
    KIND_SYNC,
    KIND_WLED_PRESET,
    MAX_ACTIONS_PER_SCENE,
    MAX_DELAY_MS,
    SCHEMA_VERSION,
    SceneService,
)


def _rl_preset_action(group_id=1, preset_slug="start_red", brightness=200, **flags):
    return {
        "kind": KIND_RL_PRESET,
        "target": {"kind": "group", "value": group_id},
        "params": {"presetId": preset_slug, "brightness": brightness},
        "flags_override": dict(flags),
    }


class SceneServiceCrudTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "scenes.json")
        self.svc = SceneService(storage_path=self.path)

    def test_empty_on_missing_file(self):
        self.assertEqual(self.svc.list(), [])
        self.assertFalse(os.path.exists(self.path))

    def test_create_persists_with_id_and_slug(self):
        scene = self.svc.create(
            label="Start Sequence",
            actions=[
                _rl_preset_action(group_id=1, arm_on_sync=True),
                {"kind": KIND_SYNC},
            ],
        )
        self.assertEqual(scene["id"], 0)
        self.assertEqual(scene["key"], "start_sequence")
        self.assertEqual(scene["label"], "Start Sequence")
        self.assertEqual(len(scene["actions"]), 2)
        self.assertEqual(scene["actions"][0]["kind"], KIND_RL_PRESET)
        self.assertEqual(scene["actions"][1]["kind"], KIND_SYNC)

        with open(self.path) as fh:
            data = json.load(fh)
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(data["next_id"], 1)
        self.assertEqual(len(data["scenes"]), 1)

    def test_create_assigns_unique_keys_on_collision(self):
        a = self.svc.create(label="Show", actions=[])
        b = self.svc.create(label="Show", actions=[])
        self.assertEqual(a["key"], "show")
        self.assertEqual(b["key"], "show_2")

    def test_get_and_get_by_id(self):
        scene = self.svc.create(label="Demo", actions=[])
        self.assertEqual(self.svc.get("demo")["id"], scene["id"])
        self.assertEqual(self.svc.get_by_id(scene["id"])["key"], "demo")
        self.assertIsNone(self.svc.get("missing"))
        self.assertIsNone(self.svc.get_by_id(9999))

    def test_update_partial_label_and_actions(self):
        scene = self.svc.create(label="Start", actions=[])
        updated = self.svc.update("start", label="Start v2")
        self.assertEqual(updated["label"], "Start v2")
        # `updated` is bumped to current ISO-second; same wall-second as
        # `created` is acceptable on fast machines (matches RLPresetsService).
        self.assertGreaterEqual(updated["updated"], scene["created"])

        updated2 = self.svc.update(
            "start",
            actions=[_rl_preset_action(group_id=2)],
        )
        self.assertEqual(len(updated2["actions"]), 1)
        # label preserved when not passed
        self.assertEqual(updated2["label"], "Start v2")

    def test_update_returns_none_when_key_missing(self):
        self.assertIsNone(self.svc.update("nope", label="x"))

    def test_delete_returns_true_only_when_present(self):
        self.svc.create(label="Demo", actions=[])
        self.assertTrue(self.svc.delete("demo"))
        self.assertFalse(self.svc.delete("demo"))
        self.assertEqual(self.svc.list(), [])

    def test_duplicate_clones_actions(self):
        original = self.svc.create(
            label="Start",
            actions=[_rl_preset_action(group_id=1), {"kind": KIND_SYNC}],
        )
        dup = self.svc.duplicate("start")
        self.assertNotEqual(dup["id"], original["id"])
        self.assertEqual(dup["label"], "Start copy")
        self.assertEqual(len(dup["actions"]), 2)

    def test_ids_are_monotonic_across_delete(self):
        a = self.svc.create(label="A", actions=[])
        self.svc.delete("a")
        b = self.svc.create(label="B", actions=[])
        self.assertEqual(a["id"], 0)
        self.assertEqual(b["id"], 1)  # not recycled


class SceneServiceValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "scenes.json")
        self.svc = SceneService(storage_path=self.path)

    def test_create_rejects_empty_label(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="", actions=[])

    def test_create_rejects_too_many_actions(self):
        too_many = [{"kind": KIND_SYNC}] * (MAX_ACTIONS_PER_SCENE + 1)
        with self.assertRaises(ValueError):
            self.svc.create(label="Long", actions=too_many)

    def test_max_actions_exactly_allowed(self):
        scene = self.svc.create(
            label="Edge",
            actions=[{"kind": KIND_SYNC}] * MAX_ACTIONS_PER_SCENE,
        )
        self.assertEqual(len(scene["actions"]), MAX_ACTIONS_PER_SCENE)

    def test_invalid_kind_rejected(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="Bad", actions=[{"kind": "unicorn"}])

    def test_delay_validates_duration(self):
        ok = self.svc.create(label="OK", actions=[{"kind": KIND_DELAY, "duration_ms": 0}])
        self.assertEqual(ok["actions"][0]["duration_ms"], 0)

        with self.assertRaises(ValueError):
            self.svc.create(label="Neg", actions=[{"kind": KIND_DELAY, "duration_ms": -1}])
        with self.assertRaises(ValueError):
            self.svc.create(label="Big", actions=[{"kind": KIND_DELAY, "duration_ms": MAX_DELAY_MS + 1}])

    def test_sync_rejects_extra_fields(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{"kind": KIND_SYNC, "target": {"kind": "group", "value": 1}}])

    def test_delay_rejects_target(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[
                {"kind": KIND_DELAY, "duration_ms": 100, "target": {"kind": "group", "value": 1}}
            ])

    def test_target_kind_validated(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_RL_PRESET,
                "target": {"kind": "broadcast", "value": None},
                "params": {"presetId": "x"},
            }])

    def test_group_target_range_enforced(self):
        # 255 is reserved for broadcast; not a valid scene target
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_RL_PRESET,
                "target": {"kind": "group", "value": 255},
                "params": {"presetId": "x"},
            }])
        # negative also rejected
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_RL_PRESET,
                "target": {"kind": "group", "value": -1},
                "params": {"presetId": "x"},
            }])

    def test_device_target_must_be_12_hex(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_RL_PRESET,
                "target": {"kind": "device", "value": "ABCDEF"},  # 6 chars, not 12
                "params": {"presetId": "x"},
            }])
        # 12 hex passes (and uppercases)
        scene = self.svc.create(label="OK", actions=[{
            "kind": KIND_RL_PRESET,
            "target": {"kind": "device", "value": "aabbccddeeff"},
            "params": {"presetId": "x"},
        }])
        self.assertEqual(scene["actions"][0]["target"]["value"], "AABBCCDDEEFF")

    def test_flags_override_filtered_to_known_keys(self):
        scene = self.svc.create(label="X", actions=[{
            "kind": KIND_RL_PRESET,
            "target": {"kind": "group", "value": 1},
            "params": {"presetId": "x"},
            "flags_override": {"arm_on_sync": True, "unknown_flag": True, "offset_mode": False},
        }])
        flags = scene["actions"][0]["flags_override"]
        self.assertIn("arm_on_sync", flags)
        self.assertIn("offset_mode", flags)
        self.assertNotIn("unknown_flag", flags)

    def test_kind_with_target_requires_target(self):
        with self.assertRaises(ValueError):
            self.svc.create(label="X", actions=[{
                "kind": KIND_RL_PRESET,
                "params": {"presetId": "x"},
            }])


class SceneServiceOnChangedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "scenes.json")
        self.svc = SceneService(storage_path=self.path)
        self.cb = MagicMock()
        self.svc.on_changed = self.cb

    def test_create_fires_on_changed(self):
        self.svc.create(label="A", actions=[])
        self.cb.assert_called_once()

    def test_update_fires_on_changed(self):
        self.svc.create(label="A", actions=[])
        self.cb.reset_mock()
        self.svc.update("a", label="A2")
        self.cb.assert_called_once()

    def test_update_missing_does_not_fire(self):
        self.cb.reset_mock()
        self.assertIsNone(self.svc.update("nope", label="x"))
        self.cb.assert_not_called()

    def test_delete_fires_on_changed(self):
        self.svc.create(label="A", actions=[])
        self.cb.reset_mock()
        self.svc.delete("a")
        self.cb.assert_called_once()

    def test_listener_exception_does_not_undo_write(self):
        self.svc.on_changed = MagicMock(side_effect=RuntimeError("listener boom"))
        scene = self.svc.create(label="A", actions=[])
        self.assertIsNotNone(self.svc.get("a"))
        self.assertEqual(scene["key"], "a")


class SceneServicePersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "scenes.json")

    def test_round_trip_through_disk(self):
        svc1 = SceneService(storage_path=self.path)
        svc1.create(
            label="Start",
            actions=[
                _rl_preset_action(group_id=1, arm_on_sync=True, offset_mode=True),
                {"kind": KIND_SYNC},
                {"kind": KIND_DELAY, "duration_ms": 1500},
                {
                    "kind": KIND_WLED_PRESET,
                    "target": {"kind": "device", "value": "AABBCCDDEEFF"},
                    "params": {"presetId": 5, "brightness": 128},
                    "flags_override": {"force_reapply": True},
                },
            ],
        )

        svc2 = SceneService(storage_path=self.path)
        scenes = svc2.list()
        self.assertEqual(len(scenes), 1)
        actions = scenes[0]["actions"]
        self.assertEqual(actions[0]["flags_override"]["arm_on_sync"], True)
        self.assertEqual(actions[0]["flags_override"]["offset_mode"], True)
        self.assertEqual(actions[1]["kind"], KIND_SYNC)
        self.assertEqual(actions[2]["duration_ms"], 1500)
        self.assertEqual(actions[3]["target"]["value"], "AABBCCDDEEFF")

    def test_replace_all_assigns_fresh_ids(self):
        svc = SceneService(storage_path=self.path)
        svc.create(label="A", actions=[])
        svc.create(label="B", actions=[])
        svc.replace_all([
            {"label": "X", "actions": []},
            {"label": "Y", "actions": []},
        ])
        scenes = svc.list()
        self.assertEqual([s["label"] for s in scenes], ["X", "Y"])
        # New ids must come AFTER previously used ids (no recycling)
        self.assertGreaterEqual(min(s["id"] for s in scenes), 2)

    def test_corrupt_file_starts_empty(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("not a JSON document {{")
        svc = SceneService(storage_path=self.path)
        self.assertEqual(svc.list(), [])


if __name__ == "__main__":
    unittest.main()
