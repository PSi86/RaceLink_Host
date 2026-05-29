"""EthernetTransport tests (Ethernet PoC).

Three layers:

  1. Identity / state-shim contract (no sockets).
  2. Send path — datagrams hit a loopback UDP "node" socket with the exact
     ``[type_full][recv3][body]`` framing the firmware will expect, and the
     send returns ``SendOutcome(code="SUCCESS")``.
  3. RX path — a crafted N2M IDENTIFY_REPLY / STATUS_REPLY datagram fed to the
     transport's socket is parsed into the same event shape the host expects
     (via ``parse_reply_event``) and tagged with ``gateway_id="ETH:<id>"``.

Plus an end-to-end test against ``scripts/mock_ethernet_node.py``: discovery
finds the mock node, status polls telemetry, and a preset is applied. All on
127.0.0.1; skipped gracefully if loopback UDP is unavailable.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import struct
import threading
import time
import unittest

from racelink.transport.ethernet_transport import EthernetTransport
from racelink.transport.gateway_events import LP

DIR_N2M = 0x80
OPC_DEVICES = 1
OPC_STATUS = 3


def _free_udp_port(host="127.0.0.1") -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_for(predicate, timeout=2.0, interval=0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class EthernetTransportIdentityTests(unittest.TestCase):

    def test_identity_and_kind(self):
        t = EthernetTransport("net-abc")
        self.assertEqual(t.ident_mac, "ETH:net-abc")
        self.assertEqual(t.network_id, "net-abc")
        self.assertEqual(t.kind, "ethernet")

    def test_state_shims_report_idle(self):
        t = EthernetTransport("net-abc")
        self.assertEqual(t.gateway_state_name, "IDLE")
        self.assertEqual(t.gateway_state_metadata_ms, 0)
        snap = t.gateway_state_snapshot()
        self.assertEqual(snap["state"], "IDLE")

    def test_state_request_is_noop_true(self):
        t = EthernetTransport("net-abc")
        self.assertTrue(t.send_state_request())


class EthernetTransportSendTests(unittest.TestCase):

    def setUp(self):
        # A loopback "node" socket the transport will send its datagrams to.
        self.node = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.node.bind(("127.0.0.1", 0))
        except OSError:
            self.skipTest("loopback UDP unavailable")
        self.node.settimeout(2.0)
        self.node_port = self.node.getsockname()[1]
        self.t = EthernetTransport(
            "net-send",
            node_port=self.node_port,
            host_port=0,
            bind_host="127.0.0.1",
            broadcast_host="127.0.0.1",
        )
        self.t.open()

    def tearDown(self):
        self.t.close()
        self.node.close()

    def test_send_get_devices_broadcast_frame(self):
        outcome = self.t.send_get_devices()
        self.assertEqual(outcome.code, "SUCCESS")
        data, _addr = self.node.recvfrom(2048)
        self.assertEqual(data[0], LP.make_type(LP.DIR_M2N, LP.OPC_DEVICES))
        self.assertEqual(data[1:4], b"\xFF\xFF\xFF")
        # body = build_get_devices_body(0, 0) == <BB 0,0
        self.assertEqual(data[4:], struct.pack("<BB", 0, 0))

    def test_send_get_status_frame(self):
        outcome = self.t.send_get_status(group_id=3, flags=0)
        self.assertEqual(outcome.code, "SUCCESS")
        data, _addr = self.node.recvfrom(2048)
        self.assertEqual(data[0], LP.make_type(LP.DIR_M2N, LP.OPC_STATUS))
        self.assertEqual(data[4:], struct.pack("<BB", 3, 0))

    def test_send_preset_frame(self):
        recv3 = b"\x11\x22\x33"
        outcome = self.t.send_preset(recv3, group_id=2, flags=1, preset_id=5, brightness=200)
        self.assertEqual(outcome.code, "SUCCESS")
        data, _addr = self.node.recvfrom(2048)
        self.assertEqual(data[0], LP.make_type(LP.DIR_M2N, LP.OPC_PRESET))
        self.assertEqual(data[1:4], recv3)
        self.assertEqual(data[4:8], struct.pack("<BBBB", 2, 1, 5, 200))


class EthernetTransportRxTests(unittest.TestCase):

    def setUp(self):
        self.t = EthernetTransport("net-rx", host_port=0, bind_host="127.0.0.1")
        try:
            self.t.open()
        except OSError:
            self.skipTest("loopback UDP unavailable")
        self.host_addr = self.t._sock.getsockname()
        self.events: list = []
        self.t.add_listener(self.events.append)
        self.t.start()
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def tearDown(self):
        self.t.close()
        self.sender.close()

    def _send_reply(self, opc: int, mac6: bytes, body: bytes):
        type_byte = DIR_N2M | (opc & 0x7F)
        header7 = mac6[-3:] + b"\xFF\xFF\xFF" + bytes([type_byte])
        frame = bytes([type_byte]) + header7 + body
        self.sender.sendto(frame, self.host_addr)

    def test_identify_reply_parsed_and_tagged(self):
        mac6 = bytes.fromhex("AABBCCDDEE01")
        body = bytes([4, 10, 1]) + mac6  # version, caps, groupId, mac6
        self._send_reply(OPC_DEVICES, mac6, body)
        self.assertTrue(_wait_for(lambda: len(self.events) >= 1))
        ev = self.events[0]
        self.assertEqual(ev["reply"], "IDENTIFY_REPLY")
        self.assertEqual(ev["mac6"], mac6)
        self.assertEqual(ev["groupId"], 1)
        self.assertEqual(ev["gateway_id"], "ETH:net-rx")
        # source IP is learned for later unicast
        self.assertIn(mac6[-3:], self.t._addr_book)

    def test_status_reply_parsed(self):
        mac6 = bytes.fromhex("AABBCCDDEE02")
        # <BBBBHbb: flags, configByte, effectId, brightness, vbat, rssi, snr
        body = struct.pack("<BBBBHbb", 0x01, 0, 7, 180, 0, 0, 0)
        self._send_reply(OPC_STATUS, mac6, body)
        self.assertTrue(_wait_for(lambda: len(self.events) >= 1))
        ev = self.events[0]
        self.assertEqual(ev["reply"], "STATUS_REPLY")
        self.assertEqual(ev["brightness"], 180)
        self.assertEqual(ev["effectId"], 7)
        self.assertEqual(ev["gateway_id"], "ETH:net-rx")


def _load_mock_node_module():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "mock_ethernet_node.py")
    spec = importlib.util.spec_from_file_location("mock_ethernet_node", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class EthernetPocE2ETests(unittest.TestCase):
    """End-to-end against the stdlib mock node on loopback."""

    def setUp(self):
        self.mod = _load_mock_node_module()
        try:
            self.node_port = _free_udp_port()
        except OSError:
            self.skipTest("loopback UDP unavailable")
        self.mac6 = bytes.fromhex("AABBCCDDEE0A")
        self.node = self.mod.MockNode(
            mac6=self.mac6, group=1, dev_type=10,
            node_port=self.node_port, bind_host="127.0.0.1",
        )
        self.node_thread = threading.Thread(target=self.node.serve, daemon=True)
        self.node_thread.start()
        time.sleep(0.1)  # let the node bind

        self.t = EthernetTransport(
            "net-e2e",
            node_port=self.node_port,
            host_port=0,
            bind_host="127.0.0.1",
            broadcast_host="127.0.0.1",
        )
        self.t.open()
        self.events: list = []
        self.t.add_listener(self.events.append)
        self.t.start()

    def tearDown(self):
        self.t.close()
        self.node.stop()
        self.node_thread.join(timeout=2.0)

    def _replies(self, kind: str):
        return [e for e in self.events if e.get("reply") == kind]

    def test_discovery_status_preset_roundtrip(self):
        # Discovery: broadcast OPC_DEVICES -> IDENTIFY_REPLY from the node.
        self.t.send_get_devices()
        self.assertTrue(_wait_for(lambda: self._replies("IDENTIFY_REPLY")))
        ident = self._replies("IDENTIFY_REPLY")[0]
        self.assertEqual(ident["mac6"], self.mac6)

        # Status: broadcast OPC_STATUS -> STATUS_REPLY with telemetry.
        self.t.send_get_status()
        self.assertTrue(_wait_for(lambda: self._replies("STATUS_REPLY")))
        status = self._replies("STATUS_REPLY")[0]
        self.assertEqual(status["brightness"], 128)  # mock node default

        # Preset (unicast via learned IP): node applies brightness.
        self.t.send_preset(self.mac6[-3:], group_id=1, flags=1, preset_id=5, brightness=222)
        self.assertTrue(_wait_for(lambda: self.node.brightness == 222))
        self.assertEqual(self.node.preset_id, 5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
