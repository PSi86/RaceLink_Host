"""Default-filled ``build_specials_state`` semantics + pair expansion.

Iteration 2 (2026-05-08) replaced the iteration-1 ``optional`` tri-
state model with the simpler "every option always has a value, default
fills when storage is silent" model. This file is the regression gate
for that semantic.
"""

from __future__ import annotations

import unittest

from racelink.domain import (
    RL_Dev_Type,
    build_specials_state,
    get_special_keys_for_caps,
)
from racelink.domain.device_types import get_dev_type_info


class BuildSpecialsStateDefaultsTests(unittest.TestCase):
    def test_wled_keys_seeded_from_schema_defaults(self):
        state = build_specials_state(RL_Dev_Type.NODE_WLED_REV5, stored={})
        # Schema-declared defaults from racelink/domain/specials.py.
        self.assertEqual(state["wled_fps"], 75)
        self.assertEqual(state["wled_abl_max_ma"], 0)
        self.assertEqual(state["wled_briS"], 128)
        self.assertEqual(state["wled_transition_ms"], 700)
        # Pair fields expand to flat keys with their per-field defaults.
        self.assertEqual(state["wled_seg0_start"], 0)
        self.assertEqual(state["wled_seg0_stop"], 0)
        self.assertEqual(state["wled_seg1_start"], 0)
        self.assertEqual(state["wled_seg1_stop"], 0)

    def test_stored_value_wins_over_default(self):
        state = build_specials_state(
            RL_Dev_Type.NODE_WLED_REV5,
            stored={"wled_fps": 60, "wled_briS": 200},
        )
        self.assertEqual(state["wled_fps"], 60)
        self.assertEqual(state["wled_briS"], 200)
        # Untouched options keep their schema defaults.
        self.assertEqual(state["wled_abl_max_ma"], 0)
        self.assertEqual(state["wled_transition_ms"], 700)

    def test_pair_partial_storage_uses_default_for_missing_field(self):
        # Iteration 2 model: each flat key is independent. Storing only
        # ``wled_seg0_start`` keeps that value and fills ``wled_seg0_stop``
        # from the schema default. (Iteration 1 dropped both halves on
        # partial storage; iteration 2 doesn't, since "absence = default".)
        state = build_specials_state(
            RL_Dev_Type.NODE_WLED_REV5,
            stored={"wled_seg0_start": 5},
        )
        self.assertEqual(state["wled_seg0_start"], 5)
        self.assertEqual(state["wled_seg0_stop"], 0)

    def test_required_startblock_keys_default_filled(self):
        # STARTBLOCK options use ``min`` as their default (no ``default``
        # field declared); the precedence rule in ``_scalar_default``
        # falls back to ``min`` when ``default`` is absent.
        state = build_specials_state(
            RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
            stored={},
        )
        self.assertEqual(state["startblock_slots"], 1)
        self.assertEqual(state["startblock_first_slot"], 1)


class SpecialKeysExpansionTests(unittest.TestCase):
    def test_wled_keys_include_pair_flat_fields(self):
        caps = get_dev_type_info(RL_Dev_Type.NODE_WLED_REV5).get("caps", [])
        keys = set(get_special_keys_for_caps(caps))
        self.assertIn("wled_fps", keys)
        self.assertIn("wled_abl_max_ma", keys)
        self.assertIn("wled_briS", keys)
        self.assertIn("wled_transition_ms", keys)
        self.assertIn("wled_seg0_start", keys)
        self.assertIn("wled_seg0_stop", keys)
        self.assertIn("wled_seg1_start", keys)
        self.assertIn("wled_seg1_stop", keys)
        # Logical (non-flat) pair name should NOT appear.
        self.assertNotIn("wled_seg0", keys)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
