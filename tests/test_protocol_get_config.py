"""Wire-protocol round-trip for OPC_GET_CONFIG.

* Request: ``build_get_config_body`` emits a single byte (the option).
* Reply: ``parse_reply_event`` decodes opcode 0x0A with a 5-byte body
  into ``GET_CONFIG_REPLY`` carrying ``option`` + ``data0..3`` (same
  shape as ``P_Config``).
"""

from __future__ import annotations

import unittest

from racelink.protocol.codec import parse_reply_event
from racelink.protocol.packets import build_get_config_body


class GetConfigBodyTests(unittest.TestCase):
    def test_request_body_is_one_byte(self):
        self.assertEqual(build_get_config_body(0x05), b"\x05")
        self.assertEqual(build_get_config_body(0x8C), b"\x8C")

    def test_request_body_masks_to_byte(self):
        self.assertEqual(build_get_config_body(0x105), b"\x05")


class GetConfigReplyParseTests(unittest.TestCase):
    def _make_frame(self, body: bytes) -> bytes:
        # 7-byte header (sender3, receiver3, type) + body + 3 trailing
        # bytes (rssi, snr, end-marker placeholder); ``parse_reply_event``
        # discards the trailing 3 bytes (data[7:-3]).
        sender = b"\xAA\xBB\xCC"
        receiver = b"\xDD\xEE\xFF"
        type_byte = bytes([0x80 | 0x0A])  # N→M | OPC_GET_CONFIG
        trailer = b"\x00\x00\x00"
        return sender + receiver + type_byte + body + trailer

    def test_decodes_5_byte_reply(self):
        body = bytes([0x05, 60, 0, 0, 0])  # option=FPS, data0=60
        frame = self._make_frame(body)
        ev = parse_reply_event(0x80 | 0x0A, frame, timestamp=0.0, host_rssi=-50, host_snr=8)
        self.assertEqual(ev["reply"], "GET_CONFIG_REPLY")
        self.assertEqual(ev["option"], 0x05)
        self.assertEqual((ev["data0"], ev["data1"], ev["data2"], ev["data3"]), (60, 0, 0, 0))

    def test_decodes_pair_reply(self):
        # Segment 0 geometry: option=0x06, start=0, stop=18 (LE).
        body = bytes([0x06, 0, 0, 18, 0])
        frame = self._make_frame(body)
        ev = parse_reply_event(0x80 | 0x0A, frame, timestamp=0.0, host_rssi=-40, host_snr=6)
        self.assertEqual(ev["reply"], "GET_CONFIG_REPLY")
        self.assertEqual(ev["option"], 0x06)
        self.assertEqual(ev["data2"], 18)

    def test_unexpected_body_length_falls_back_to_raw(self):
        body = b"\x05\x00\x00"  # too short
        frame = self._make_frame(body)
        ev = parse_reply_event(0x80 | 0x0A, frame, timestamp=0.0, host_rssi=0, host_snr=0)
        self.assertEqual(ev["reply"], "GET_CONFIG_REPLY")
        self.assertEqual(ev["body_raw"], body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
