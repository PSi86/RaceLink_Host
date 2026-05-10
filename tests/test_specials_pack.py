"""Wire-byte packing for OPC_CONFIG override option values.

Validates :meth:`SpecialsService.pack_option_value` for the three
shapes the host emits: 1-byte scalar, 2-byte LE scalar, and the
``uint16-pair`` (segment-geometry) shape used by options 0x06 / 0x07.

The byte order matches the canonical spec at
``RaceLink_Docs/docs/reference/wire-protocol.md`` §``P_Config``
(little-endian for every multi-byte field).
"""

from __future__ import annotations

import unittest

from racelink.protocol.packets import build_config_body
from racelink.services.specials_service import SpecialsService


class PackOptionValueTests(unittest.TestCase):
    svc = SpecialsService(rl_instance=None)

    def test_scalar_one_byte(self):
        opt = {"key": "wled_fps", "option": 0x05, "bytes": 1}
        d0, d1, d2, d3 = self.svc.pack_option_value(opt, 60)
        self.assertEqual((d0, d1, d2, d3), (60, 0, 0, 0))

    def test_scalar_two_bytes_little_endian(self):
        opt = {"key": "wled_abl_max_ma", "option": 0x08, "bytes": 2}
        d0, d1, d2, d3 = self.svc.pack_option_value(opt, 500)  # 0x01F4
        self.assertEqual((d0, d1, d2, d3), (0xF4, 0x01, 0, 0))

    def test_uint16_pair_emits_le_pair(self):
        opt = {
            "key": "wled_seg0",
            "option": 0x06,
            "bytes": 4,
            "shape": "uint16-pair",
            "fields": [
                {"name": "start"},
                {"name": "stop"},
            ],
        }
        d0, d1, d2, d3 = self.svc.pack_option_value(opt, {"start": 0, "stop": 18})
        self.assertEqual((d0, d1, d2, d3), (0, 0, 18, 0))

        d0, d1, d2, d3 = self.svc.pack_option_value(opt, {"start": 256, "stop": 1023})
        # 256 = 0x0100 → (00, 01); 1023 = 0x03FF → (FF, 03)
        self.assertEqual((d0, d1, d2, d3), (0x00, 0x01, 0xFF, 0x03))

    def test_pack_round_trips_through_build_config_body(self):
        opt = {"key": "wled_transition_ms", "option": 0x0A, "bytes": 2}
        d0, d1, d2, d3 = self.svc.pack_option_value(opt, 1234)  # 0x04D2
        body = build_config_body(option=0x0A, data0=d0, data1=d1, data2=d2, data3=d3)
        self.assertEqual(body, bytes([0x0A, 0xD2, 0x04, 0x00, 0x00]))


class ValidateOptionValueTests(unittest.TestCase):
    svc = SpecialsService(rl_instance=None)

    def test_scalar_range_check(self):
        opt = {"key": "wled_fps", "option": 0x05, "bytes": 1, "min": 0, "max": 250}
        self.svc.validate_option_value(opt, 60)
        with self.assertRaises(ValueError):
            self.svc.validate_option_value(opt, -1)
        with self.assertRaises(ValueError):
            self.svc.validate_option_value(opt, 251)

    def test_pair_range_check(self):
        opt = {
            "key": "wled_seg0",
            "option": 0x06,
            "bytes": 4,
            "shape": "uint16-pair",
            "fields": [
                {"name": "start", "min": 0, "max": 65535},
                {"name": "stop", "min": 0, "max": 65535},
            ],
        }
        self.svc.validate_option_value(opt, {"start": 0, "stop": 18})
        with self.assertRaises(ValueError):
            self.svc.validate_option_value(opt, {"start": 0})  # missing stop
        with self.assertRaises(ValueError):
            self.svc.validate_option_value(opt, 42)  # not a dict
        with self.assertRaises(ValueError):
            self.svc.validate_option_value(opt, {"start": -1, "stop": 18})


class StoredValueFromSpecialsTests(unittest.TestCase):
    svc = SpecialsService(rl_instance=None)

    def test_scalar_round_trip(self):
        opt = {"key": "wled_fps", "option": 0x05, "bytes": 1}
        self.assertEqual(self.svc.stored_value_from_specials(opt, {"wled_fps": 60}), 60)
        self.assertIsNone(self.svc.stored_value_from_specials(opt, {}))

    def test_pair_returns_none_when_either_half_missing(self):
        opt = {
            "key": "wled_seg0",
            "option": 0x06,
            "shape": "uint16-pair",
            "fields": [{"name": "start"}, {"name": "stop"}],
        }
        self.assertEqual(
            self.svc.stored_value_from_specials(opt, {"wled_seg0_start": 0, "wled_seg0_stop": 18}),
            {"start": 0, "stop": 18},
        )
        self.assertIsNone(
            self.svc.stored_value_from_specials(opt, {"wled_seg0_start": 0}),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
