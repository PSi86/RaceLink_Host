"""Domain models for RaceLink."""

from __future__ import annotations

import time
import uuid
from typing import Optional

from .device_types import RL_FLAG_POWER_ON


def default_device_name(mac: str) -> str:
    """Default display name for a device, derived from its MAC.

    Shared between the IDENTIFY path (first appearance) and the
    rename-reset path (operator clears the name to revert to default).
    The format is ``"WLED <12 hex chars upper>"``; non-hex separators
    in ``mac`` (``:``, ``-``) are stripped before formatting.
    """
    cleaned = "".join(c for c in (mac or "") if c.isalnum()).upper()
    return f"WLED {cleaned[-12:]}"


class RL_Device:
    def __init__(
        self,
        addr: str,
        dev_type: int,
        name: str,
        groupId: int = 0,
        version: int = 0,
        caps: int = 0,
        voltage_mV: int = 0,
        node_rssi: int = 0,
        node_snr: int = 0,
        flags: int = RL_FLAG_POWER_ON,
        presetId: int = 1,
        brightness: int = 70,
        configByte: int = 0,
        effectId: int = 0,
        network_id: Optional[str] = None,
        last_known_rf_config: Optional[dict] = None,
    ):
        self.addr: str = addr
        self.dev_type: int = int(dev_type)
        self.name: str = name
        self.version: int = int(version)
        self.caps: int = int(caps)
        self.groupId: int = int(groupId)
        # Multi-network attributes (Stage 2). ``network_id`` is the
        # stable UUID of the RL_Network this device is bound to;
        # ``None`` means "no network assigned yet" (will land in the
        # default network on the next persistence migration).
        # ``last_known_rf_config`` is the single-value snapshot of
        # the PHY settings the device reported on its last
        # IDENTIFY_REPLY / OPC_GET_RF_CONFIG reply — used by the
        # Setup-Change-Assistant (Stage 3+) for diff detection.
        self.network_id: Optional[str] = str(network_id) if network_id else None
        self.last_known_rf_config: Optional[dict] = (
            dict(last_known_rf_config) if isinstance(last_known_rf_config, dict) else None
        )

        # presetId: last preset we asked the device to load (set by send paths).
        # effectId: device's currently active segment mode (set by STATUS_REPLY;
        #          was carried in the same wire byte as presetId before the
        #          2026-04-25 rename, but the semantics differ).
        self.flags: int = int(flags) & 0xFF
        self.presetId: int = int(presetId) & 0xFF
        self.effectId: int = int(effectId) & 0xFF
        self.brightness: int = int(brightness) & 0xFF
        self.configByte: int = int(configByte) & 0xFF
        self.specials: dict[str, int] = {}

        self.voltage_mV: int = int(voltage_mV)
        self.node_rssi: int = int(node_rssi)
        self.node_snr: int = int(node_snr)

        self.host_rssi: int = 0
        self.host_snr: int = 0
        self.last_seen_ts = 0

        self.link_online = None  # type: Optional[bool]
        self.link_ts = 0.0
        self.link_error = None
        self.last_ack = {"ok": False, "opcode": None, "status": None, "seq": None, "ts": 0.0}

    def update_from_identify(self, version, dev_type, groupId, mac6_bytes, host_rssi=None, host_snr=None):
        self.version = int(version) if version is not None else self.version
        if dev_type is not None:
            self.caps = int(dev_type)
            self.dev_type = int(dev_type)

        if self.groupId == 0 and groupId:
            self.groupId = int(groupId) & 0xFF

        if host_rssi is not None:
            self.host_rssi = int(host_rssi)
        if host_snr is not None:
            self.host_snr = int(host_snr)

        self.last_seen_ts = time.time()

        try:
            self.mark_online()
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            pass

    def update_from_status(self, flags, configByte, effectId, brightness, vbat_mV, node_rssi, node_snr, host_rssi=None, host_snr=None):
        # P_StatusReply byte 2 was renamed presetId -> effectId 2026-04-25
        # (semantics shift to active segment mode index). presetId is no
        # longer touched by status; it remains the host-tracked "what preset
        # have I told this device to load".
        self.flags = int(flags) & 0xFF if flags is not None else self.flags
        self.configByte = int(configByte) & 0xFF if configByte is not None else self.configByte
        self.effectId = int(effectId) & 0xFF if effectId is not None else self.effectId
        self.brightness = int(brightness) & 0xFF if brightness is not None else self.brightness

        self.voltage_mV = int(vbat_mV) if vbat_mV is not None else self.voltage_mV
        self.node_rssi = int(node_rssi) if node_rssi is not None else self.node_rssi
        self.node_snr = int(node_snr) if node_snr is not None else self.node_snr
        if host_rssi is not None:
            self.host_rssi = int(host_rssi)
        if host_snr is not None:
            self.host_snr = int(host_snr)

        self.last_seen_ts = time.time()

        try:
            self.mark_online()
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            pass

    def mark_online(self) -> None:
        self.link_online = True
        self.link_ts = time.time()
        self.link_error = None

    def mark_offline(self, reason: str = "") -> None:
        self.link_online = False
        self.link_ts = time.time()
        self.link_error = reason or None

    def ack_clear(self) -> None:
        self.last_ack = {"ok": False, "opcode": None, "status": None, "seq": None, "ts": 0.0}

    def ack_update(self, opcode: int, status: int, seq=None, host_rssi=None, host_snr=None) -> None:
        self.last_ack = {
            "ok": int(status) == 0,
            "opcode": int(opcode),
            "status": int(status),
            "seq": int(seq) if seq is not None else None,
            "ts": time.time(),
        }
        if host_rssi is not None:
            self.host_rssi = int(host_rssi)
        if host_snr is not None:
            self.host_snr = int(host_snr)

        self.last_seen_ts = time.time()
        try:
            self.mark_online()
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            pass

    def ack_ok(self) -> bool:
        return bool(self.last_ack["ok"])


class RL_DeviceGroup:
    def __init__(
        self,
        name: str,
        static_group: int = 0,
        dev_type: int = 0,
        network_id: Optional[str] = None,
    ):
        self.name: str = name
        self.static_group: int = static_group
        self.dev_type: int = int(dev_type)
        # Multi-network: a group never spans networks (hard-enforced
        # in Stage 3's bulk_set_group validation). ``None`` means
        # "default network" pre-migration; v1->v2 persistence
        # migration backfills this from the singleton default
        # network so every persisted group carries a stable id.
        self.network_id: Optional[str] = str(network_id) if network_id else None


class RL_Network:
    """A logical RaceLink network — one LoRa channel + sync-word band that
    devices and a gateway share. Hardware MAC of the bound gateway is
    stored so a hardware swap (same identity, different physical unit)
    is just a re-bind, not a re-create.

    Stable across reboots; ``id`` is a UUID4 string. ``rf_config`` mirrors
    the wire-format P_RfConfig dict (freq_hz / bw_khz_x10 / sf / cr_den /
    sync_word / tx_power_dbm / preamble) so the network and the actual
    radio carrying it speak the same field shape. Stage 3 introduces a
    ``channel_id`` indirection on top (pointer into the region channel
    table); for Stage 2 the field exists but is always ``None``.
    """

    def __init__(
        self,
        id: Optional[str] = None,
        name: str = "Default",
        gateway_mac: Optional[str] = None,
        region: str = "EU868",
        channel_id: Optional[int] = None,
        rf_config: Optional[dict] = None,
        created_ts: Optional[float] = None,
    ):
        self.id: str = str(id) if id else str(uuid.uuid4())
        self.name: str = str(name)
        self.gateway_mac: Optional[str] = str(gateway_mac).upper() if gateway_mac else None
        self.region: str = str(region)
        self.channel_id: Optional[int] = int(channel_id) if channel_id is not None else None
        self.rf_config: Optional[dict] = dict(rf_config) if isinstance(rf_config, dict) else None
        self.created_ts: float = float(created_ts) if created_ts is not None else time.time()
