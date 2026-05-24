"""Tests for the per-scene ``network_scope`` field in scenes_service.

Pins:

  * Default mode is ``auto`` for new + legacy-loaded scenes.
  * Validator accepts canonical shapes, rejects malformed ones.
  * Round-trip preserves order, dedupes, trims whitespace.
  * Update can flip between auto and explicit modes.
  * Duplicate copies the scope verbatim.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from racelink.services.scenes_service import (
    SceneService,
    _canonical_network_scope,
)


class _SceneFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.svc = SceneService(
            storage_path=os.path.join(self._tmp.name, "scenes.json"),
        )


class CanonicalNetworkScopeTests(unittest.TestCase):

    def test_none_defaults_to_auto(self):
        self.assertEqual(_canonical_network_scope(None), {"mode": "auto"})

    def test_explicit_auto_preserves_mode_and_strips_stray_ids(self):
        # A round-trip from the editor commonly leaves a stale network_ids
        # list when the operator switches back to auto. The canonicalizer
        # strips it so the persisted shape stays clean.
        self.assertEqual(
            _canonical_network_scope({"mode": "auto", "network_ids": ["x"]}),
            {"mode": "auto"},
        )

    def test_explicit_mode_round_trip(self):
        out = _canonical_network_scope(
            {"mode": "explicit", "network_ids": ["net-a", "net-b"]}
        )
        self.assertEqual(out, {"mode": "explicit", "network_ids": ["net-a", "net-b"]})

    def test_explicit_mode_dedupes_and_preserves_first_occurrence(self):
        out = _canonical_network_scope(
            {"mode": "explicit", "network_ids": ["net-b", "net-a", "net-b", "net-c"]}
        )
        self.assertEqual(out["network_ids"], ["net-b", "net-a", "net-c"])

    def test_explicit_mode_trims_whitespace(self):
        out = _canonical_network_scope(
            {"mode": "explicit", "network_ids": ["  net-a  ", "net-b\t"]}
        )
        self.assertEqual(out["network_ids"], ["net-a", "net-b"])

    def test_explicit_mode_empty_list_rejected(self):
        with self.assertRaises(ValueError):
            _canonical_network_scope({"mode": "explicit", "network_ids": []})

    def test_explicit_mode_only_blanks_rejected(self):
        with self.assertRaises(ValueError):
            _canonical_network_scope(
                {"mode": "explicit", "network_ids": ["", "   ", None]}  # type: ignore
            )

    def test_non_list_network_ids_rejected(self):
        with self.assertRaises(ValueError):
            _canonical_network_scope({"mode": "explicit", "network_ids": "net-a"})

    def test_non_string_entry_rejected(self):
        with self.assertRaises(ValueError):
            _canonical_network_scope(
                {"mode": "explicit", "network_ids": ["net-a", 42]}  # type: ignore
            )

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            _canonical_network_scope({"mode": "lol", "network_ids": ["a"]})

    def test_non_dict_input_rejected(self):
        with self.assertRaises(ValueError):
            _canonical_network_scope("auto")  # type: ignore


class ScenesServiceScopeRoundTripTests(_SceneFixture):

    def test_create_defaults_to_auto(self):
        scene = self.svc.create(label="A", actions=[])
        self.assertEqual(scene["network_scope"], {"mode": "auto"})

    def test_create_with_explicit_scope_persists(self):
        scene = self.svc.create(
            label="A",
            actions=[],
            network_scope={"mode": "explicit", "network_ids": ["net-a"]},
        )
        self.assertEqual(
            scene["network_scope"],
            {"mode": "explicit", "network_ids": ["net-a"]},
        )

    def test_create_rejects_malformed_scope(self):
        with self.assertRaises(ValueError):
            self.svc.create(
                label="A", actions=[],
                network_scope={"mode": "explicit", "network_ids": []},
            )

    def test_update_can_set_explicit_scope(self):
        self.svc.create(label="A", actions=[], key="a")
        updated = self.svc.update(
            "a",
            network_scope={"mode": "explicit", "network_ids": ["net-a", "net-b"]},
        )
        self.assertEqual(
            updated["network_scope"],
            {"mode": "explicit", "network_ids": ["net-a", "net-b"]},
        )

    def test_update_can_flip_back_to_auto(self):
        self.svc.create(
            label="A", actions=[], key="a",
            network_scope={"mode": "explicit", "network_ids": ["net-a"]},
        )
        updated = self.svc.update("a", network_scope={"mode": "auto"})
        self.assertEqual(updated["network_scope"], {"mode": "auto"})

    def test_update_without_scope_kwarg_keeps_existing(self):
        # None means "don't change" — same convention as stop_on_error.
        self.svc.create(
            label="A", actions=[], key="a",
            network_scope={"mode": "explicit", "network_ids": ["net-a"]},
        )
        updated = self.svc.update("a", label="A2")
        self.assertEqual(
            updated["network_scope"],
            {"mode": "explicit", "network_ids": ["net-a"]},
        )

    def test_duplicate_copies_explicit_scope(self):
        src = self.svc.create(
            label="A", actions=[], key="a",
            network_scope={"mode": "explicit", "network_ids": ["net-a", "net-b"]},
        )
        dup = self.svc.duplicate("a")
        self.assertEqual(dup["network_scope"], src["network_scope"])

    def test_duplicate_copies_auto_scope(self):
        self.svc.create(label="A", actions=[], key="a")
        dup = self.svc.duplicate("a")
        self.assertEqual(dup["network_scope"], {"mode": "auto"})

    def test_legacy_scene_without_field_loads_as_auto(self):
        # Simulate a legacy persisted scene file by writing the JSON
        # directly (bypassing the service), then re-loading.
        import json
        path = self.svc._path  # noqa: SLF001 — test introspection
        legacy = {
            "schema_version": 1,
            "next_id": 1,
            "scenes": [{
                "id": 0, "key": "legacy", "label": "Legacy",
                "created": "2026-01-01T00:00:00",
                "updated": "2026-01-01T00:00:00",
                "actions": [],
                "stop_on_error": True,
                # Deliberately no network_scope field.
            }],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(legacy, fh)
        # Force reload.
        self.svc._invalidate()  # noqa: SLF001
        loaded = self.svc.get("legacy")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["network_scope"], {"mode": "auto"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
