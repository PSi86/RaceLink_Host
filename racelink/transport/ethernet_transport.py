"""UDP/IP transport for RaceLink Ethernet devices (Ethernet PoC).

Duck-typed twin of :class:`racelink.transport.gateway_serial.GatewaySerialTransport`.
It speaks the *same* RaceLink wire opcodes and emits the *same* RX event shape
(:func:`racelink.protocol.codec.parse_reply_event`), so the discovery / status
services and the ``PendingMatcher`` registry route Ethernet replies through the
exact same machinery as RF — only the framing/medium changes from USB-CDC to UDP.

Where the LoRa gateway is a separate hardware unit with its own MAC, an Ethernet
network's "gateway" is the host NIC itself. There is therefore no gateway-MAC to
bind against: the transport carries a synthetic, stable ident
(``"ETH:<network_id>"``) for event tagging / matcher buckets, and the controller
routes to it via the directly-stamped ``network_id`` (the network_id-first branch
in ``Controller.transport_for_network``).

Wire framing (UDP gives message boundaries — no ``[0x00][LEN]`` byte framing):

* **Host -> Node (M2N)**: datagram = ``[type_full(1)][recv3(3)][body(N)]`` — the
  same payload the serial ``_send_m2n`` writes. ``recv3`` is the last 3 MAC bytes
  (or broadcast ``FF FF FF``). Unicast goes to the learned node IP; broadcast goes
  to the configured broadcast address.
* **Node -> Host (N2M)**: datagram = ``[type_byte(1)][Header7(7)][body(N)]``
  (``Header7`` = ``sender3(3)+receiver3(3)+type(1)`` like the LoRa PHY). The
  transport appends a synthetic ``rssi=0 (LE16) + snr=0 (i8)`` trailer and calls
  ``parse_reply_event`` so the parsed event is byte-for-byte what the host expects.

Opcode coverage: full M2N parity with the serial transport — discovery,
status, preset, set-group, control, sync, offset, config, get-config,
indicate, headless, and stream all flow over UDP (the firmware's W5500
backend feeds the same backend-agnostic dispatch as LoRa). The LoRa-PHY-only
RF-config opcodes (``send_rf_config`` / ``send_get_rf_config_to_node``) return
``USB_ERROR`` — an Ethernet network has no radio. Device firmware update over
RaceLink is out of scope (WLED's own Wi-Fi/web OTA handles that).
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

from .framing import mac_last3_from_hex
from .gateway_events import GATEWAY_STATE_IDLE, GATEWAY_STATE_NAME
from .gateway_serial import SendOutcome
from ..protocol.codec import parse_reply_event
from ..protocol.packets import (
    build_get_devices_body,
    build_preset_body,
    build_set_group_body,
    build_indicate_body,
    build_control_body,
    build_config_body,
    build_get_config_body,
    build_sync_body,
    build_offset_body,
)

# ``LP`` is the auto-generated protocol mirror (make_type / DIR_* / OPC_*).
from .gateway_events import LP

logger = logging.getLogger("racelink_transport")

# Default UDP ports. The node listens on NODE_PORT for M2N commands; the host
# binds HOST_PORT to receive N2M replies. Distinct defaults so a node and the
# host can run on the same machine (loopback PoC) without a port clash.
DEFAULT_NODE_PORT = 5078
DEFAULT_HOST_PORT = 5079
DEFAULT_BIND_HOST = "0.0.0.0"
DEFAULT_BROADCAST_HOST = "255.255.255.255"

# Broadcast sentinel — last-3 MAC ``FF FF FF`` addresses every node on the
# network (mirrors the LoRa broadcast target).
_BROADCAST_RECV3 = b"\xFF\xFF\xFF"

# RX socket poll cadence. recvfrom uses this timeout so ``close()`` makes the
# reader notice ``_stop`` within one tick instead of blocking forever.
_RX_POLL_TIMEOUT_S = 0.3


class EthernetTransport:
    """UDP transport for one Ethernet RaceLink network (host NIC = network).

    Constructed per Ethernet ``RL_Network``; ``network_id`` is stamped directly
    so ``Controller.transport_for_network`` resolves to it without a gateway MAC.
    """

    def __init__(
        self,
        network_id: str,
        *,
        node_port: int = DEFAULT_NODE_PORT,
        host_port: int = DEFAULT_HOST_PORT,
        bind_host: str = DEFAULT_BIND_HOST,
        broadcast_host: str = DEFAULT_BROADCAST_HOST,
        discovery: str = "broadcast",
        on_event=None,
    ):
        self.network_id = str(network_id)
        # Synthetic, hardware-independent ident used for event tagging and the
        # PendingMatcher per-gateway bucket. Stable across reboots (derived from
        # the persisted network id), and never collides with a real gateway MAC.
        self.ident_mac = f"ETH:{self.network_id}"
        # Discriminator so the controller's attach path can branch RF vs Ethernet.
        self.kind = "ethernet"

        self.node_port = int(node_port)
        self.host_port = int(host_port)
        self.bind_host = str(bind_host)
        self.broadcast_host = str(broadcast_host)
        self.discovery = str(discovery)
        self.on_event = on_event

        # ``port`` mirrors the serial transport attribute (used in log labels /
        # UI); a human-readable "host:port" stands in for the device path.
        self.port = f"{self.bind_host}:{self.host_port}"

        self._sock: socket.socket | None = None
        self._stop = False
        self._rx_thread: threading.Thread | None = None
        self._q: list = []
        self._qmax = 1000
        self._listeners: list = []
        self._tx_listeners: list = []
        # mac_last3 (3 bytes) -> (ip, port). Learned from N2M reply source
        # addresses so subsequent unicast sends reach the right node.
        self._addr_book: dict[bytes, tuple[str, int]] = {}
        self._addr_lock = threading.Lock()
        # Parity with the serial transport's PORT_BUSY signalling.
        self.last_discovery_had_busy_port = False

    # ---- gateway state shims ---------------------------------------------
    #
    # Ethernet has no LoRa state machine (RX_WINDOW / LBT / TX). Report a
    # constant IDLE so the master-pill / state consumers in the backend and
    # frontend render a stable "ready" state instead of UNKNOWN.

    @property
    def gateway_state_byte(self) -> int:
        return int(GATEWAY_STATE_IDLE)

    @property
    def gateway_state_name(self) -> str:
        return GATEWAY_STATE_NAME.get(int(GATEWAY_STATE_IDLE), "IDLE")

    @property
    def gateway_state_metadata_ms(self) -> int:
        return 0

    def gateway_state_snapshot(self) -> dict:
        return {
            "state_byte": self.gateway_state_byte,
            "state": self.gateway_state_name,
            "state_metadata_ms": 0,
        }

    # ---- lifecycle --------------------------------------------------------

    def _log_label(self) -> str:
        return f"[ETH {self.network_id[:8]}]"

    def discover_and_open(self) -> bool:
        """Bind the UDP socket. Returns ``True`` on success (parity with the
        serial transport's discover_and_open contract).

        There is no per-transport device probe here — Ethernet discovery is a
        *device-level* OPC_DEVICES broadcast issued later by the discovery
        service, not a transport-enumeration step.
        """
        self.last_discovery_had_busy_port = False
        try:
            self.open()
        except OSError:
            logger.warning(
                "%s failed to bind UDP %s:%d",
                self._log_label(), self.bind_host, self.host_port, exc_info=True,
            )
            return False
        return True

    def open(self) -> None:
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((self.bind_host, self.host_port))
        sock.settimeout(_RX_POLL_TIMEOUT_S)
        self._sock = sock
        logger.info(
            "%s UDP transport bound on %s:%d (node_port=%d, broadcast=%s)",
            self._log_label(), self.bind_host, self.host_port,
            self.node_port, self.broadcast_host,
        )

    def start(self) -> None:
        if self._sock is None:
            self.open()
        self._stop = False
        self._rx_thread = threading.Thread(
            target=self._reader,
            daemon=True,
            name=f"rl-eth-rx-{self.network_id[:8]}",
        )
        self._rx_thread.start()

    def close(self) -> None:
        self._stop = True
        if self._rx_thread:
            self._rx_thread.join(timeout=1.0)
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                logger.debug(
                    "%s UDP socket close failed", self._log_label(), exc_info=True,
                )
            self._sock = None

    # ---- listeners --------------------------------------------------------

    def add_listener(self, cb):
        if cb and cb not in self._listeners:
            self._listeners.append(cb)

    def remove_listener(self, cb):
        try:
            if cb in self._listeners:
                self._listeners.remove(cb)
        except Exception:
            # swallow-ok: best-effort
            pass

    def add_tx_listener(self, cb):
        if cb and cb not in self._tx_listeners:
            self._tx_listeners.append(cb)

    def remove_tx_listener(self, cb):
        try:
            if cb in self._tx_listeners:
                self._tx_listeners.remove(cb)
        except Exception:
            # swallow-ok: best-effort
            pass

    def _emit(self, ev: dict):
        # Tag every RX event with this transport's identity so the
        # PendingMatcher gateway_id filter routes correctly (identical contract
        # to the serial transport's ``_emit``).
        ev.setdefault("gateway_id", self.ident_mac)
        if len(self._q) < self._qmax:
            self._q.append(ev)
        for cb in list(self._listeners):
            try:
                cb(ev)
            except Exception:
                logger.debug(
                    "%s RX listener raised: ev_type=%r",
                    self._log_label(), ev.get("type"), exc_info=True,
                )
        if self.on_event and self.on_event not in self._listeners:
            try:
                self.on_event(ev)
            except Exception:
                logger.debug(
                    "%s on_event handler raised: ev_type=%r",
                    self._log_label(), ev.get("type"), exc_info=True,
                )

    def _emit_tx(self, ev: dict):
        ev.setdefault("gateway_id", self.ident_mac)
        for cb in list(self._tx_listeners):
            try:
                cb(ev)
            except Exception:
                logger.debug(
                    "%s TX listener raised: ev_type=%r",
                    self._log_label(), ev.get("type"), exc_info=True,
                )

    def drain_events(self, timeout_s: float = 0.0):
        t0 = time.time()
        out = []
        while True:
            if self._q:
                out.append(self._q.pop(0))
            else:
                if time.time() - t0 >= timeout_s:
                    break
                time.sleep(0.01)
        return out

    # ---- send path --------------------------------------------------------

    def _target_for(self, recv3: bytes) -> tuple[str, int]:
        """Resolve the UDP destination for ``recv3``.

        Broadcast (``FF FF FF``) goes to the configured broadcast host; a known
        unicast recv3 goes to its learned IP. Unknown unicast falls back to the
        broadcast host (the matching node will still pick it up by recv3).
        """
        if recv3 == _BROADCAST_RECV3:
            return (self.broadcast_host, self.node_port)
        with self._addr_lock:
            known = self._addr_book.get(bytes(recv3))
        if known is not None:
            return (known[0], self.node_port)
        return (self.broadcast_host, self.node_port)

    def _send_m2n(self, type_full: int, recv3: bytes, body: bytes = b"") -> SendOutcome:
        """Send one M2N datagram. UDP/RESP_NONE = fire-and-forget: there is no
        gateway TX-outcome to wait for, so a successful ``sendto`` returns
        ``SUCCESS`` immediately (a socket error returns ``USB_ERROR``).
        """
        if len(recv3) != 3:
            raise ValueError("recv3 must be 3 bytes")
        if self._sock is None:
            return SendOutcome.usb_error(detail="socket not open")

        payload = bytes([type_full & 0xFF]) + bytes(recv3) + (body or b"")
        dest = self._target_for(recv3)

        opc = type_full & 0x7F
        self._emit_tx({
            "type": "TX_M2N",
            "type_full": type_full,
            "dir": type_full & 0x80,
            "opc": opc,
            "recv3": bytes(recv3),
            "body_len": len(body or b""),
        })
        try:
            self._sock.sendto(payload, dest)
            outcome = SendOutcome.success()
        except OSError as e:
            logger.warning(
                "%s UDP send failed (opc=0x%02X dest=%s): %s",
                self._log_label(), opc, dest, e,
            )
            outcome = SendOutcome.usb_error(detail=str(e))

        self._emit_tx({
            "type": "TX_OUTCOME",
            "type_full": type_full,
            "dir": type_full & 0x80,
            "opc": opc,
            "recv3": bytes(recv3),
            "outcome": outcome.code,
            "reason": outcome.reason,
            "reason_name": outcome.reason_name,
            "detail": outcome.detail,
        })
        return outcome

    def send_get_devices(self, recv3=_BROADCAST_RECV3, group_id=0, flags=0) -> SendOutcome:
        body = build_get_devices_body(group_id=group_id, flags=flags)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_DEVICES), recv3, body)

    def send_get_status(self, recv3=_BROADCAST_RECV3, group_id=0, flags=0) -> SendOutcome:
        body = struct.pack("<BB", int(group_id) & 0xFF, int(flags) & 0xFF)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_STATUS), recv3, body)

    def send_preset(self, recv3: bytes, group_id: int, flags: int, preset_id: int, brightness: int) -> SendOutcome:
        body = build_preset_body(group_id=group_id, flags=flags, preset_id=preset_id, brightness=brightness)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_PRESET), recv3, body)

    def send_wled_preset(self, recv3: bytes, group_id: int, state: int, preset_id: int, brightness: int) -> SendOutcome:
        """Legacy helper: WLED preset with a simple on/off state flag (-> OPC_PRESET)."""
        flags = 0x01 if int(state) else 0x00
        return self.send_preset(recv3, group_id, flags, int(preset_id), int(brightness))

    def send_set_group(self, recv3: bytes, group_id: int) -> SendOutcome:
        body = build_set_group_body(group_id)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_SET_GROUP), recv3, body)

    def send_indicate(self, recv3: bytes, indicator_type: int, duration_sec: int) -> SendOutcome:
        body = build_indicate_body(indicator_type=indicator_type, duration_sec=duration_sec)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_INDICATE), recv3, body)

    def send_control(self, recv3: bytes, group_id: int, flags: int, **params) -> SendOutcome:
        """OPC_CONTROL (variable-length 3..21 B body). Kwargs match build_control_body."""
        body = build_control_body(group_id=group_id, flags=flags, **params)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_CONTROL), recv3, body)

    def send_config(self, recv3: bytes = _BROADCAST_RECV3, option: int = 0,
                    data0: int = 0, data1: int = 0, data2: int = 0, data3: int = 0) -> SendOutcome:
        body = build_config_body(option=option, data0=data0, data1=data1, data2=data2, data3=data3)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_CONFIG), recv3, body)

    def send_get_config(self, recv3: bytes, option: int) -> SendOutcome:
        """OPC_GET_CONFIG (1 B body = option). Unicast-only; reply is a 5 B P_Config."""
        body = build_get_config_body(option=option)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_GET_CONFIG), recv3, body)

    def send_sync(self, recv3: bytes = _BROADCAST_RECV3, ts24: int = 0,
                  brightness: int = 0, flags: int = 0) -> SendOutcome:
        body = build_sync_body(ts24=ts24, brightness=brightness, flags=flags)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_SYNC), recv3, body)

    def send_offset(self, recv3: bytes = _BROADCAST_RECV3, group_id: int = 0,
                    mode="none", **mode_params) -> SendOutcome:
        """OPC_OFFSET (variable-length 2..7 B body). See build_offset_body for modes."""
        body = build_offset_body(group_id=group_id, mode=mode, **mode_params)
        return self._send_m2n(LP.make_type(LP.DIR_M2N, LP.OPC_OFFSET), recv3, body)

    def send_stream(self, recv3: bytes, payload: bytes) -> SendOutcome:
        """Send a logical stream payload as OPC_STREAM datagrams.

        On RF the gateway splits the payload into radio-sized P_Stream packets;
        on Ethernet there is no gateway, so the host does that chunking here:
        8-byte data chunks (max 16 = 128 B) framed as ``[ctrl][data(8)]`` with
        ctrl = start(0x80) | stop(0x40) | packets_left(0x3F), matching the
        firmware's handleStreamPacket() reassembly. Each chunk is one M2N
        datagram; the first failed send short-circuits and is returned.
        """
        data = bytes(payload or b"")
        CHUNK = 8
        total = max(1, (len(data) + CHUNK - 1) // CHUNK)
        if total > 16:
            return SendOutcome.usb_error(detail=f"stream payload too large ({len(data)} B > 128)")
        padded = data + b"\x00" * (total * CHUNK - len(data))
        type_full = LP.make_type(LP.DIR_M2N, LP.OPC_STREAM)
        outcome = SendOutcome.success()
        for i in range(total):
            packets_left = (total - 1) - i
            ctrl = (0x80 if i == 0 else 0) | (0x40 if i == total - 1 else 0) | (packets_left & 0x3F)
            body = bytes([ctrl]) + padded[i * CHUNK:(i + 1) * CHUNK]
            outcome = self._send_m2n(type_full, recv3, body)
            if outcome.code != SendOutcome.success().code:
                return outcome
        return outcome

    # ---- RF config: not applicable on Ethernet (LoRa PHY only) -------------
    def send_rf_config(self, recv3: bytes, rf_config: dict) -> SendOutcome:
        return SendOutcome.usb_error(detail="RF config not supported on Ethernet networks")

    def send_get_rf_config_to_node(self, recv3: bytes) -> SendOutcome:
        return SendOutcome.usb_error(detail="RF config not supported on Ethernet networks")

    def send_state_request(self) -> bool:
        """No-op for Ethernet (no gateway state machine). Returns ``True`` so
        the controller's post-attach fire-and-forget call is a clean success.
        """
        return True

    # ---- RX reader thread -------------------------------------------------

    def _reader(self):
        while not self._stop:
            sock = self._sock
            if sock is None:
                break
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if self._stop:
                    break
                logger.debug("%s recvfrom raised", self._log_label(), exc_info=True)
                continue
            if not data:
                continue
            try:
                self._handle_datagram(data, addr)
            except Exception:
                logger.exception(
                    "%s _handle_datagram raised on %d bytes; dropping",
                    self._log_label(), len(data),
                )

    def _handle_datagram(self, data: bytes, addr) -> None:
        type_byte = data[0]
        # Only N2M replies are processed (host is the M2N originator). Bit 7
        # set == DIR_N2M (DIR_N2M = 0x80, DIR_M2N = 0x00).
        if (type_byte & 0x80) != LP.DIR_N2M:
            return
        rest = data[1:]
        if len(rest) < 7:
            return
        # Learn the sender's IP for later unicast (sender3 = Header7[0:3]).
        sender3 = bytes(rest[0:3])
        try:
            with self._addr_lock:
                self._addr_book[sender3] = (addr[0], int(addr[1]))
        except Exception:
            # swallow-ok: address learning is best-effort
            pass
        # parse_reply_event expects ``data = Header7 + body + trailer(3)`` where
        # the trailer is rssi(LE16)+snr(i8). Ethernet has no RF metrics, so
        # append a zero trailer; host_rssi/host_snr are reported as 0.
        ev = parse_reply_event(
            type_byte,
            bytes(rest) + b"\x00\x00\x00",
            timestamp=time.time(),
            host_rssi=0,
            host_snr=0,
        )
        self._emit(ev)
