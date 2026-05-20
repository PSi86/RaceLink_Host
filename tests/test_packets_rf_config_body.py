"""Codec tests for the P_RfConfig wire body (Stage 1.5 prerequisites).

The wire layout is shared between three call sites:
  * USB-CDC GW_CMD_SET_RF_CONFIG payload + EV_RF_CHANGED body tail
    (``gateway_serial.py``).
  * LoRa OPC_RF_CONFIG request body (``transport.send_rf_config``).
  * LoRa OPC_GET_RF_CONFIG reply body (``codec.parse_reply_event``).

The single source of truth lives in ``racelink.protocol.packets``;
these tests pin the byte-for-byte contract so a refactor that
re-orders or re-types any field fails fast.
"""

from __future__ import annotations

import struct
import unittest

from racelink.protocol.packets import (
    RF_CONFIG_FIELDS,
    RF_CONFIG_STRUCT,
    build_get_rf_config_body,
    build_rf_config_body,
    unpack_rf_config_body,
)


_CANONICAL = {
    "freq_hz":      867_700_000,
    "bw_khz_x10":   1250,           # 125.0 kHz
    "sf":           7,
    "cr_den":       5,              # 4/5
    "sync_word":    0x12,
    "tx_power_dbm": 14,
    "preamble":     8,
}


class PRfConfigStructTests(unittest.TestCase):

    def test_struct_size_is_12(self):
        # The C struct in racelink_proto.h carries a static_assert
        # demanding exactly 12 B; the Python mirror must match.
        self.assertEqual(RF_CONFIG_STRUCT.size, 12)

    def test_fields_order(self):
        # Field tuple order matches the C struct member order. A
        # re-order on either side breaks every wire send.
        self.assertEqual(
            RF_CONFIG_FIELDS,
            ("freq_hz", "bw_khz_x10", "sf", "cr_den", "sync_word",
             "tx_power_dbm", "preamble"),
        )

    def test_build_canonical_matches_expected_bytes(self):
        body = build_rf_config_body(_CANONICAL)
        self.assertEqual(len(body), 12)
        # Expected little-endian byte layout:
        #   freq_hz = 867_700_000 = 0x33B80D20 -> 20 0D B8 33
        #   bw_khz_x10 = 1250 = 0x04E2 -> E2 04
        #   sf = 7 -> 07
        #   cr_den = 5 -> 05
        #   sync_word = 0x12 -> 12
        #   tx_power_dbm = 14 -> 0E (signed; positive stays 0x0E)
        #   preamble = 8 -> 08 00
        expected = bytes.fromhex("200DB833 E204 07 05 12 0E 0800".replace(" ", ""))
        self.assertEqual(body, expected)

    def test_build_then_unpack_roundtrip(self):
        body = build_rf_config_body(_CANONICAL)
        got = unpack_rf_config_body(body)
        self.assertEqual(got, _CANONICAL)

    def test_tx_power_negative_serializes_as_signed_int8(self):
        # int8 range: -128..127. -9 dBm (the min the SX1262 supports)
        # must land as 0xF7 on the wire, not as 0xFFFFFFF7 (no widening).
        cfg = dict(_CANONICAL)
        cfg["tx_power_dbm"] = -9
        body = build_rf_config_body(cfg)
        # The tx_power byte sits at offset 4+2+1+1+1 = 9.
        self.assertEqual(body[9], 0xF7)
        # Roundtrip back to -9 (signed decode).
        got = unpack_rf_config_body(body)
        self.assertEqual(got["tx_power_dbm"], -9)

    def test_tx_power_out_of_int8_range_raises(self):
        # struct.pack raises struct.error for an int8 overflow; callers
        # are expected to range-check first.
        cfg = dict(_CANONICAL)
        cfg["tx_power_dbm"] = 200  # > +127
        with self.assertRaises(struct.error):
            build_rf_config_body(cfg)

    def test_build_missing_field_raises_key_error(self):
        cfg = dict(_CANONICAL)
        del cfg["sf"]
        with self.assertRaises(KeyError):
            build_rf_config_body(cfg)

    def test_unpack_short_input_raises(self):
        with self.assertRaises(struct.error):
            unpack_rf_config_body(b"\x00\x01\x02")

    def test_unpack_extra_bytes_ignored(self):
        # The unpack helper consumes exactly the first 12 B; the codec
        # for OPC_GET_RF_CONFIG_REPLY passes the body slice, but we
        # tolerate a slightly oversized buffer here for defence.
        body = build_rf_config_body(_CANONICAL) + b"\xDE\xAD\xBE\xEF"
        got = unpack_rf_config_body(body)
        self.assertEqual(got, _CANONICAL)


class PGetRfConfigBodyTests(unittest.TestCase):

    def test_default_reserved_is_zero(self):
        self.assertEqual(build_get_rf_config_body(), b"\x00")

    def test_reserved_byte_passes_through(self):
        # Future field — today must stay 0 per firmware contract, but
        # the codec itself doesn't enforce that (the node-side handler
        # does; see WLED racelink_wled.cpp OPC_GET_RF_CONFIG case).
        self.assertEqual(build_get_rf_config_body(reserved=0xAB), b"\xAB")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
