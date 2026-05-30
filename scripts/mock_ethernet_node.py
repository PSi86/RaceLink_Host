#!/usr/bin/env python3
"""Mock RaceLink Ethernet node (UDP) for the EthernetTransport PoC.

Stdlib-only standalone device emulator. It speaks the same RaceLink wire
opcodes the host's :class:`racelink.transport.ethernet_transport.EthernetTransport`
sends, so a real host can discover it, poll its status, and apply presets over
UDP — no firmware required.

Wire framing (matches EthernetTransport):

* Host -> Node (M2N, bit7 clear): ``[type_full(1)][recv3(3)][body(N)]``.
  ``recv3`` is the node's last-3 MAC or the broadcast sentinel ``FF FF FF``.
* Node -> Host (N2M, bit7 set):  ``[type_byte(1)][Header7(7)][body(N)]``
  where ``Header7 = sender3(3) + receiver3(3) + type(1)``. Replies are sent
  back to the datagram's source address (the host's bound port).

Supported opcodes (PoC scope):
  * OPC_DEVICES (0x01) -> IDENTIFY_REPLY (version, caps, groupId, mac6)
  * OPC_STATUS  (0x03) -> STATUS_REPLY  (flags, configByte, effectId, brightness, vbat, rssi, snr)
  * OPC_PRESET  (0x04) -> applied locally (logged), no reply (RESP_NONE)

Run several instances with different ``--mac`` / ``--node-port`` to emulate a
small fleet.

Example:
    py scripts/mock_ethernet_node.py --mac AABBCCDDEE01 --group 1 --node-port 5078
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

# Wire constants (mirror racelink_proto: DIR_M2N=0x00, DIR_N2M=0x80).
DIR_M2N = 0x00
DIR_N2M = 0x80
OPC_DEVICES = 0x01
OPC_SET_GROUP = 0x02
OPC_STATUS = 0x03
OPC_PRESET = 0x04

BROADCAST_RECV3 = b"\xFF\xFF\xFF"

# IDENTIFY_REPLY firmware version + status defaults the mock reports.
MOCK_FW_VERSION = 4


def _mac_bytes(mac_hex: str) -> bytes:
    clean = mac_hex.strip().replace(":", "").replace("-", "").upper()
    if len(clean) != 12:
        raise ValueError(f"--mac must be 12 hex chars (got {mac_hex!r})")
    return bytes.fromhex(clean)


class MockNode:
    def __init__(self, *, mac6: bytes, group: int, dev_type: int,
                 node_port: int, bind_host: str):
        self.mac6 = mac6
        self.last3 = mac6[-3:]
        self.group = group & 0xFF
        self.dev_type = dev_type & 0xFF
        self.node_port = node_port
        self.bind_host = bind_host
        # Mutable state the host can drive / read back.
        self.brightness = 128
        self.effect_id = 0
        self.preset_id = 0
        self.power_on = 1
        self.sock: socket.socket | None = None
        self._running = True

    def stop(self) -> None:
        """Signal :meth:`serve` to exit its loop (used by the e2e test)."""
        self._running = False

    def _addressed_to_me(self, recv3: bytes) -> bool:
        return recv3 == BROADCAST_RECV3 or recv3 == self.last3

    def _header7(self, type_byte: int, receiver3: bytes = BROADCAST_RECV3) -> bytes:
        # sender3 = my MAC last3; receiver3 = host/broadcast; trailing type echo.
        return self.last3 + receiver3 + bytes([type_byte & 0xFF])

    def _reply(self, opc: int, body: bytes, dest) -> None:
        type_byte = DIR_N2M | (opc & 0x7F)
        frame = bytes([type_byte]) + self._header7(type_byte) + body
        assert self.sock is not None
        self.sock.sendto(frame, dest)

    def _handle(self, data: bytes, addr) -> None:
        if len(data) < 4:
            return
        type_full = data[0]
        if (type_full & 0x80) != DIR_M2N:
            return  # not a host->node command
        opc = type_full & 0x7F
        recv3 = bytes(data[1:4])
        body = bytes(data[4:])
        if not self._addressed_to_me(recv3):
            return

        if opc == OPC_DEVICES:
            # IDENTIFY_REPLY body: [version, caps, groupId, mac6(6)]
            reply_body = bytes([MOCK_FW_VERSION, self.dev_type, self.group]) + self.mac6
            self._reply(OPC_DEVICES, reply_body, addr)
            print(f"[{self.mac6.hex().upper()}] IDENTIFY -> host {addr}")
        elif opc == OPC_STATUS:
            # STATUS_REPLY body (8 B): <BBBBHbb
            flags = 0x01 if self.power_on else 0x00
            config_byte = 0
            vbat_mV = 0
            rssi = 0
            snr = 0
            reply_body = struct.pack(
                "<BBBBHbb", flags, config_byte, self.effect_id,
                self.brightness, vbat_mV, rssi, snr,
            )
            self._reply(OPC_STATUS, reply_body, addr)
            print(f"[{self.mac6.hex().upper()}] STATUS -> bri={self.brightness} effect={self.effect_id}")
        elif opc == OPC_PRESET:
            # P_Preset body (4 B): group, flags, presetId, brightness. RESP_NONE.
            if len(body) >= 4:
                _grp, flags, preset_id, brightness = body[0], body[1], body[2], body[3]
                self.preset_id = preset_id
                self.brightness = brightness
                self.power_on = 1 if (flags & 0x01) else self.power_on
                print(f"[{self.mac6.hex().upper()}] PRESET applied preset={preset_id} bri={brightness} flags=0x{flags:02X}")
        elif opc == OPC_SET_GROUP:
            if len(body) >= 1:
                self.group = body[0] & 0xFF
                print(f"[{self.mac6.hex().upper()}] SET_GROUP -> {self.group}")
        else:
            print(f"[{self.mac6.hex().upper()}] ignoring opc=0x{opc:02X}")

    def serve(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((self.bind_host, self.node_port))
        sock.settimeout(0.5)
        self.sock = sock
        print(
            f"Mock node {self.mac6.hex().upper()} listening on "
            f"{self.bind_host}:{self.node_port} (group={self.group}, dev_type={self.dev_type})"
        )
        try:
            while self._running:
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                try:
                    self._handle(data, addr)
                except Exception as e:  # pragma: no cover - defensive
                    print(f"[{self.mac6.hex().upper()}] handler error: {e}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\nshutting down")
        finally:
            sock.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Mock RaceLink Ethernet node (UDP)")
    p.add_argument("--mac", default="AABBCCDDEE01", help="12 hex chars (default AABBCCDDEE01)")
    p.add_argument("--group", type=int, default=0, help="reported groupId (default 0)")
    p.add_argument("--dev-type", type=int, default=10, help="device caps/type byte (default 10)")
    p.add_argument("--node-port", type=int, default=5078, help="UDP port to listen on (default 5078)")
    p.add_argument("--bind-host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    args = p.parse_args(argv)

    node = MockNode(
        mac6=_mac_bytes(args.mac),
        group=args.group,
        dev_type=args.dev_type,
        node_port=args.node_port,
        bind_host=args.bind_host,
    )
    node.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
