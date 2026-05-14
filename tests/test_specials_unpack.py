"""Inverse of pack_option_value: data0..3 → schema-shaped value.

Used by ``/api/specials/get`` to turn ``GET_CONFIG_REPLY`` data bytes
back into the operator-facing value (scalar or pair). The pack/unpack
pair must round-trip cleanly for every WLED + STARTBLOCK option in
``RL_SPECIALS``.
"""

from __future__ import annotations

import unittest

from racelink.services.specials_service import SpecialsService


class UnpackOptionValueTests(unittest.TestCase):
    svc = SpecialsService(rl_instance=None)

    def test_scalar_one_byte(self):
        opt = {"key": "wled_fps", "option": 0x05, "bytes": 1}
        self.assertEqual(self.svc.unpack_option_value(opt, 60), 60)
        self.assertEqual(self.svc.unpack_option_value(opt, 75, 0, 0, 0), 75)

    def test_scalar_two_bytes_little_endian(self):
        opt = {"key": "wled_abl_max_ma", "option": 0x08, "bytes": 2}
        # 500 = 0x01F4 → bytes (F4, 01)
        self.assertEqual(self.svc.unpack_option_value(opt, 0xF4, 0x01, 0, 0), 500)

    def test_uint16_pair_decodes_le_pair(self):
        opt = {
            "key": "wled_seg0",
            "option": 0x06,
            "bytes": 4,
            "shape": "uint16-pair",
            "fields": [{"name": "start"}, {"name": "stop"}],
        }
        self.assertEqual(
            self.svc.unpack_option_value(opt, 0, 0, 18, 0),
            {"start": 0, "stop": 18},
        )
        self.assertEqual(
            self.svc.unpack_option_value(opt, 0x00, 0x01, 0xFF, 0x03),
            {"start": 256, "stop": 1023},
        )

    def test_round_trip_via_pack(self):
        opt_scalar = {"key": "wled_briS", "option": 0x09, "bytes": 1}
        d0, d1, d2, d3 = self.svc.pack_option_value(opt_scalar, 200)
        self.assertEqual(self.svc.unpack_option_value(opt_scalar, d0, d1, d2, d3), 200)

        opt_2b = {"key": "wled_transition_ms", "option": 0x0A, "bytes": 2}
        d0, d1, d2, d3 = self.svc.pack_option_value(opt_2b, 1234)
        self.assertEqual(self.svc.unpack_option_value(opt_2b, d0, d1, d2, d3), 1234)

        opt_pair = {
            "key": "wled_seg1",
            "option": 0x07,
            "bytes": 4,
            "shape": "uint16-pair",
            "fields": [{"name": "start"}, {"name": "stop"}],
        }
        d0, d1, d2, d3 = self.svc.pack_option_value(opt_pair, {"start": 9, "stop": 27})
        self.assertEqual(
            self.svc.unpack_option_value(opt_pair, d0, d1, d2, d3),
            {"start": 9, "stop": 27},
        )


class WriteSpecialsTests(unittest.TestCase):
    svc = SpecialsService(rl_instance=None)

    def test_writes_scalar_into_dev_specials(self):
        class FakeDev:
            specials: dict = {}
        dev = FakeDev()
        opt = {"key": "wled_fps", "option": 0x05, "bytes": 1}
        written = self.svc.write_specials(dev, opt, 60)
        self.assertEqual(written, ["wled_fps"])
        self.assertEqual(dev.specials, {"wled_fps": 60})

    def test_writes_pair_as_two_flat_keys(self):
        class FakeDev:
            specials: dict = {}
        dev = FakeDev()
        opt = {
            "key": "wled_seg0",
            "option": 0x06,
            "shape": "uint16-pair",
            "fields": [{"name": "start"}, {"name": "stop"}],
        }
        written = self.svc.write_specials(dev, opt, {"start": 0, "stop": 18})
        self.assertEqual(set(written), {"wled_seg0_start", "wled_seg0_stop"})
        self.assertEqual(dev.specials["wled_seg0_start"], 0)
        self.assertEqual(dev.specials["wled_seg0_stop"], 18)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
