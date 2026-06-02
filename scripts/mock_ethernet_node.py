#!/usr/bin/env python3
"""Mock RaceLink Ethernet node (UDP) — full RaceLink opcode emulator.

Stdlib-only standalone device emulator. It speaks the same RaceLink wire
opcodes the host's :class:`racelink.transport.ethernet_transport.EthernetTransport`
sends and answers them exactly as the real firmware does over the W5500/UDP
backend (which feeds the same backend-agnostic ``handlePacket`` dispatch as the
LoRa build), so a real host can discover, poll, apply presets, set groups,
control, sync, offset, configure, read config, indicate, and stream — over UDP,
no firmware required.

Wire framing (matches EthernetTransport):

* Host -> Node (M2N, bit7 clear): ``[type_full(1)][recv3(3)][body(N)]``.
  ``recv3`` is the node's last-3 MAC or the broadcast sentinel ``FF FF FF``.
* Node -> Host (N2M, bit7 set):  ``[type_byte(1)][Header7(7)][body(N)]``
  where ``Header7 = sender3(3) + receiver3(3) + type(1)``. Replies are sent
  back to the datagram's source address (the host's bound port).

Supported opcodes (response policy in parentheses — mirrors the proto rules):
  * OPC_DEVICES   (0x01, SPECIFIC) -> IDENTIFY_REPLY (version, caps, groupId, mac6)
  * OPC_SET_GROUP (0x02, ACK)      -> apply groupId + ACK
  * OPC_STATUS    (0x03, SPECIFIC) -> STATUS_REPLY (flags, configByte, effectId, brightness, vbat, rssi, snr)
  * OPC_PRESET    (0x04, NONE)     -> apply locally, no reply
  * OPC_CONFIG    (0x05, ACK)      -> store option/data, ACK
  * OPC_SYNC      (0x06, NONE)     -> apply locally, no reply
  * OPC_STREAM    (0x07, ACK)      -> reassemble chunks, ACK on the stop packet
  * OPC_CONTROL   (0x08, NONE)     -> apply locally, no reply
  * OPC_OFFSET    (0x09, NONE)     -> apply locally, no reply
  * OPC_GET_CONFIG(0x0A, SPECIFIC) -> GET_CONFIG_REPLY (5 B P_Config)
  * OPC_HEADLESS  (0x0B, NONE)     -> apply locally, no reply
  * OPC_INDICATE  (0x0C, NONE)     -> apply locally, no reply

RF-config opcodes (OPC_RF_CONFIG / OPC_GET_RF_CONFIG) are LoRa-PHY only and are
deliberately not emulated — the host never sends them to an Ethernet node.

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
OPC_CONFIG = 0x05
OPC_SYNC = 0x06
OPC_STREAM = 0x07
OPC_CONTROL = 0x08
OPC_OFFSET = 0x09
OPC_GET_CONFIG = 0x0A
OPC_HEADLESS = 0x0B
OPC_INDICATE = 0x0C
OPC_ACK = 0x7E

BROADCAST_RECV3 = b"\xFF\xFF\xFF"

# ACK status byte (0 == ACK_OK, mirrors the firmware's ack_status contract).
ACK_OK = 0x00

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
        # Per-option config store (option -> (data0, data1, data2, data3)),
        # written by OPC_CONFIG and read back by OPC_GET_CONFIG.
        self.config_store: dict[int, tuple[int, int, int, int]] = {}
        # Last fully-reassembled OPC_STREAM payload (bytes) for assertions.
        self.last_stream_payload: bytes | None = None
        self._stream_buf = bytearray()
        # Log of every addressed M2N frame as (opc, body) so a test can assert
        # which operations reached the node (incl. the RESP_NONE ones that
        # produce no reply: CONTROL / SYNC / OFFSET / INDICATE / HEADLESS).
        self.recv_log: list[tuple[int, bytes]] = []
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

    def _ack(self, ack_of: int, dest, status: int = ACK_OK) -> None:
        """Send an OPC_ACK for ``ack_of`` (P_Ack = [ack_of, status])."""
        self._reply(OPC_ACK, bytes([ack_of & 0x7F, status & 0xFF]), dest)
        print(f"[{self.mac6.hex().upper()}] ACK opc=0x{ack_of:02X} status={status}")

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

        self.recv_log.append((opc, body))

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
            # P_SetGroup body (1 B): groupId. ACK after applying.
            if len(body) >= 1:
                self.group = body[0] & 0xFF
                print(f"[{self.mac6.hex().upper()}] SET_GROUP -> {self.group}")
            self._ack(OPC_SET_GROUP, addr)
        elif opc == OPC_CONFIG:
            # P_Config body (5 B): option + data0..3. Store + ACK.
            if len(body) >= 5:
                self.config_store[body[0]] = (body[1], body[2], body[3], body[4])
                print(f"[{self.mac6.hex().upper()}] CONFIG opt={body[0]} -> {self.config_store[body[0]]}")
            self._ack(OPC_CONFIG, addr)
        elif opc == OPC_GET_CONFIG:
            # Body (1 B): option to read. Reply 5 B P_Config (zeros if unset).
            option = body[0] if body else 0
            d0, d1, d2, d3 = self.config_store.get(option, (0, 0, 0, 0))
            self._reply(OPC_GET_CONFIG, bytes([option, d0, d1, d2, d3]), addr)
            print(f"[{self.mac6.hex().upper()}] GET_CONFIG opt={option} -> ({d0},{d1},{d2},{d3})")
        elif opc == OPC_STREAM:
            # P_Stream chunk: [ctrl(1)][data(8)]. ctrl bit7=start, bit6=stop,
            # low 6 bits = packets_left. Reassemble; ACK on the stop chunk
            # (mirrors the firmware's handleStreamPacket completion ACK).
            if body:
                ctrl = body[0]
                chunk = body[1:9]
                if ctrl & 0x80:  # start packet
                    self._stream_buf = bytearray()
                self._stream_buf.extend(chunk)
                if ctrl & 0x40:  # stop packet
                    self.last_stream_payload = bytes(self._stream_buf)
                    print(f"[{self.mac6.hex().upper()}] STREAM complete ({len(self.last_stream_payload)} B)")
                    self._ack(OPC_STREAM, addr)
        elif opc == OPC_CONTROL:
            # P_Control (variable): direct effect-parameter remote control.
            # RESP_NONE — recorded in recv_log; apply brightness if present.
            print(f"[{self.mac6.hex().upper()}] CONTROL ({len(body)} B)")
        elif opc == OPC_SYNC:
            print(f"[{self.mac6.hex().upper()}] SYNC ({len(body)} B)")
        elif opc == OPC_OFFSET:
            print(f"[{self.mac6.hex().upper()}] OFFSET ({len(body)} B)")
        elif opc == OPC_INDICATE:
            print(f"[{self.mac6.hex().upper()}] INDICATE ({len(body)} B)")
        elif opc == OPC_HEADLESS:
            print(f"[{self.mac6.hex().upper()}] HEADLESS ({len(body)} B)")
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
