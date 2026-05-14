"""Persistence round-trip regression — iteration 10's Bug A.

Iter 10 fixed a long-standing bug where every host reload (or manual
``/api/reload``) silently dropped the device's persisted ``specials``
to schema defaults. The cause: ``build_specials_state(type_id,
stored)`` reads ``stored.get(flat_key)`` flat at the top level, but
the loader was passing the full persisted device record where the
specials live nested under ``stored["specials"]``. Every option fell
back to the schema default, including the segment-geometry pair
fields that have no meaningful default (the operator's segment-LED
count was lost on every reload).

This file is the regression gate: a non-default specials dict must
survive ``dump_state → load_state → build_specials_state`` round-trip
unchanged.
"""

from __future__ import annotations

import unittest

from racelink.domain import (
    RL_Dev_Type,
    build_specials_state,
)
from racelink.domain.specials import create_device
from racelink.state.persistence import dump_state, load_state


class BuildSpecialsStateAcceptsBothShapesTests(unittest.TestCase):
    """``build_specials_state`` must accept either the canonical specials
    sub-dict OR the full persisted device record (defensive unwrap)."""

    NON_DEFAULT_SPECIALS = {
        "wled_fps": 60,
        "wled_abl_max_ma": 500,
        "wled_briS": 200,
        "wled_transition_ms": 1500,
        "wled_seg0_start": 0,
        "wled_seg0_stop": 200,
        "wled_seg1_start": 200,
        "wled_seg1_stop": 400,
    }

    def _expect_non_default(self, state: dict) -> None:
        for key, expected in self.NON_DEFAULT_SPECIALS.items():
            self.assertEqual(
                state.get(key), expected,
                f"{key} must round-trip; got {state.get(key)!r}, expected {expected}",
            )

    def test_canonical_shape_specials_subdict(self):
        # The canonical contract — pass the flat specials sub-dict.
        state = build_specials_state(
            RL_Dev_Type.NODE_WLED_REV5,
            self.NON_DEFAULT_SPECIALS,
        )
        self._expect_non_default(state)

    def test_full_device_record_shape_unwrap(self):
        # The historical loader call shape — pass the full device
        # record with specials nested under ``specials``. Pre-iter-10
        # this silently returned all defaults; the defensive unwrap
        # makes it work.
        device_record = {
            "addr": "AABBCCDDEEFF",
            "name": "Test WLED",
            "groupId": 3,
            "flags": 0,
            "presetId": 0,
            "effectId": 0,
            "brightness": 128,
            "specials": dict(self.NON_DEFAULT_SPECIALS),
            "voltage_mV": 0,
            "version": 7,
        }
        state = build_specials_state(RL_Dev_Type.NODE_WLED_REV5, device_record)
        self._expect_non_default(state)

    def test_empty_record_falls_back_to_schema_defaults(self):
        # Sanity: an empty record (or a record without the specials key)
        # falls back cleanly to schema defaults; no crash.
        state = build_specials_state(RL_Dev_Type.NODE_WLED_REV5, {})
        # FPS default is 75 from the schema (iter-7).
        self.assertEqual(state["wled_fps"], 75)


class PersistenceRoundTripTests(unittest.TestCase):
    """End-to-end: a device with non-default specials must survive
    ``dump_state → load_state → build_specials_state`` unchanged.
    Reproduces the exact data flow of ``save_to_db`` followed by
    ``load_from_db`` (the latter is what fires on host restart and
    on the operator's "Reload" button)."""

    def test_wled_seg_geometry_survives_dump_load_roundtrip(self):
        # Build a fresh WLED device with non-default segment geometry
        # — the field that motivated iter-10.
        dev = create_device(
            addr="AABBCCDDEEFF",
            dev_type=RL_Dev_Type.NODE_WLED_REV5,
            name="Test WLED",
            groupId=3,
            specials={
                "wled_seg0_start": 0,
                "wled_seg0_stop": 200,
                "wled_seg1_start": 200,
                "wled_seg1_stop": 400,
                "wled_fps": 60,
            },
        )
        # ``dump_state`` mirrors ``save_to_db``'s payload composition.
        raw = dump_state([dev], [], schema_version=1)

        loaded_devices, _, _ = load_state(raw)
        self.assertEqual(len(loaded_devices), 1)
        loaded_record = loaded_devices[0]

        # The loader path — ``build_specials_state`` with the full
        # device record. Iter-10's defensive unwrap makes this work;
        # pre-iter-10 it returned schema defaults and silently lost
        # the segment values.
        state = build_specials_state(
            RL_Dev_Type.NODE_WLED_REV5,
            loaded_record.get("specials") or {},
        )
        self.assertEqual(state["wled_seg0_start"], 0)
        self.assertEqual(state["wled_seg0_stop"], 200)
        self.assertEqual(state["wled_seg1_start"], 200)
        self.assertEqual(state["wled_seg1_stop"], 400)
        self.assertEqual(state["wled_fps"], 60)

    def test_startblock_specials_survives_dump_load_roundtrip(self):
        # Same regression for STARTBLOCK options. Pre-iter-10 the
        # silent default-fallback was less visible because the
        # default (1) often matched what the operator had saved.
        dev = create_device(
            addr="00112233445566",
            dev_type=RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
            name="Test SB",
            groupId=5,
            specials={
                "startblock_slots": 4,
                "startblock_first_slot": 2,
            },
        )
        raw = dump_state([dev], [], schema_version=1)
        loaded_devices, _, _ = load_state(raw)
        loaded_record = loaded_devices[0]

        state = build_specials_state(
            RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
            loaded_record.get("specials") or {},
        )
        self.assertEqual(state["startblock_slots"], 4)
        self.assertEqual(state["startblock_first_slot"], 2)

    def test_loader_with_full_record_uses_unwrap(self):
        # Belt-and-suspenders: even if a future call site forgets the
        # ``.get('specials')`` and passes the full record, the
        # defensive unwrap in ``build_specials_state`` recovers the
        # values. Iter-10's "Bug A" was exactly this slip.
        dev = create_device(
            addr="AABBCCDDEEFF",
            dev_type=RL_Dev_Type.NODE_WLED_REV5,
            name="Test WLED",
            groupId=1,
            specials={"wled_seg0_start": 0, "wled_seg0_stop": 200},
        )
        raw = dump_state([dev], [], schema_version=1)
        loaded_devices, _, _ = load_state(raw)
        loaded_record = loaded_devices[0]

        # Pass the FULL record (the iter-pre-10 mistaken shape).
        state = build_specials_state(RL_Dev_Type.NODE_WLED_REV5, loaded_record)
        self.assertEqual(state["wled_seg0_start"], 0)
        self.assertEqual(state["wled_seg0_stop"], 200)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
