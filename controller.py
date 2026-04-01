from __future__ import annotations

import logging
import re
import threading
import time
import json
from typing import Dict, Optional, Tuple, Union

from .services import ControlService, DeviceService, TransportService

from .ui import RaceLinkUIMixin
from RHUI import UIFieldSelectOption
from .data import (
    RL_Device,
    RL_DeviceGroup,
    RL_Dev_Type,
    build_specials_state,
    create_device,
    get_dev_type_info,
    RL_FLAG_HAS_BRI,
    RL_FLAG_POWER_ON,
    rl_backup_devicelist,
    rl_backup_grouplist,
    rl_devicelist,
    rl_grouplist,
)

# ---- lora proto registry (auto-generated from lora_proto.h) ----
try:
    from . import lora_proto_auto as LPA
except Exception:
    import lora_proto_auto as LPA

# ---- transport import (tolerant to both package and flat layout) ----
try:
    from .racelink_transport import (
        LP,
        EV_ERROR,
        EV_RX_WINDOW_CLOSED,
        EV_RX_WINDOW_OPEN,
        LoRaUSB,
        _mac_last3_from_hex,
    )
except Exception:
    from racelink_transport import (
        LP,
        EV_ERROR,
        EV_RX_WINDOW_CLOSED,
        EV_RX_WINDOW_OPEN,
        LoRaUSB,
        _mac_last3_from_hex,
    )

logger = logging.getLogger(__name__)

_STARTBLOCK_VER = 0x01

_DE_UMLAUT_MAP = {
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "ß": "ss",
    "Ä": "AE",
    "Ö": "OE",
    "Ü": "UE",
}

_ALLOWED_NAME_RE = re.compile(r"[^A-Z0-9 _\-\.\+]", re.IGNORECASE)


def _sanitize_pilot_name(name: str, max_len: int = 32) -> str:
    if not name:
        return ""
    for k, v in _DE_UMLAUT_MAP.items():
        name = name.replace(k, v)
    name = name.strip().upper()
    name = _ALLOWED_NAME_RE.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len] if max_len > 0 else name


def _encode_channel_fixed2(label: str) -> bytes:
    """
    Immer exakt 2 Bytes ASCII.
    - upper()
    - padding mit '-' falls zu kurz
    - truncate falls zu lang
    """
    lab = (label or "").strip().upper()
    lab2 = (lab + "--")[:2]
    return lab2.encode("ascii", errors="replace")


def build_startblock_payload_v1(
    slot: int,
    channel_label: str,
    pilot_name: str,
    max_name_len: int = 32,
    name_encoding: str = "ascii",
) -> bytes:
    """
    [ver][slot][chan2][name_len u8][name bytes]
    """
    slot_b = max(0, min(255, int(slot)))
    chan2 = _encode_channel_fixed2(channel_label)

    clean_name = _sanitize_pilot_name(pilot_name, max_len=max_name_len)

    if name_encoding.lower() == "utf-8":
        name_bytes = clean_name.encode("utf-8", errors="replace")
    else:
        name_bytes = clean_name.encode("ascii", errors="replace")

    if len(name_bytes) > 255:
        name_bytes = name_bytes[:255]

    out = bytearray()
    out.append(_STARTBLOCK_VER)
    out.append(slot_b)
    out.extend(chan2)  # 2 bytes
    out.append(len(name_bytes))  # 1 byte
    out.extend(name_bytes)
    return bytes(out)


class RaceLink_LoRa(RaceLinkUIMixin):
    def __init__(self, rhapi, name, label):
        self._rhapi = rhapi
        self.name = name
        self.label = label
        self.lora = None
        self.ready = False
        self.action_reg_fn = None
        self.deviceCfgValid = False
        self.groupCfgValid = False
        self.uiDeviceList = None
        self.uiGroupList = None
        self.uiDiscoveryGroupList = None

        # Transport-level pending expectation (for online/offline determination).
        self._pending_expect = None  # dict with keys: dev, rule, opcode7, sender_last3, ts

        self._transport_hooks_installed = False
        self._pending_config = {}
        self._reconnect_in_progress = False
        self._last_reconnect_ts = 0.0
        self._last_error_notify_ts = 0.0
        # Basic colors: 1-9; Basic effects: 10-19; Special Effects (WLED only): 20-100
        self.uiEffectList = [
            UIFieldSelectOption("01", "Red"),
            UIFieldSelectOption("02", "Green"),
            UIFieldSelectOption("03", "Blue"),
            UIFieldSelectOption("04", "White"),
            UIFieldSelectOption("05", "Yellow"),
            UIFieldSelectOption("06", "Cyan"),
            UIFieldSelectOption("07", "Magenta"),
            UIFieldSelectOption("10", "Blink Multicolor"),
            UIFieldSelectOption("11", "Pulse White"),
            UIFieldSelectOption("12", "Colorloop"),
            UIFieldSelectOption("13", "Blink RGB"),
            UIFieldSelectOption("20", "WLED Chaser"),
            UIFieldSelectOption("21", "WLED Chaser inverted"),
            UIFieldSelectOption("22", "WLED Rainbow"),
        ]

        self.transport_service = TransportService(self)
        self.device_service = DeviceService(self)
        self.control_service = ControlService(self)

    def onStartup(self, _args):
        self.load_from_db()
        self.discoverPort({})

    def save_to_db(self, args):
        logger.debug("RL: Writing current states to Database")
        config_str_devices = str([obj.__dict__ for obj in rl_devicelist])
        self._rhapi.db.option_set("rl_device_config", config_str_devices)

        if len(rl_grouplist) >= len(rl_backup_grouplist):
            config_str_groups = str([obj.__dict__ for obj in rl_grouplist])
        else:
            config_str_groups = str([obj.__dict__ for obj in rl_backup_grouplist])
        self._rhapi.db.option_set("rl_groups_config", config_str_groups)

    def load_from_db(self):
        logger.debug("RL: Applying config from Database")
        config_str_devices = self._rhapi.db.option("rl_device_config", None)
        config_str_groups = self._rhapi.db.option("rl_groups_config", None)

        if config_str_devices is None:
            config_str_devices = str([obj.__dict__ for obj in rl_backup_devicelist])
            self._rhapi.db.option_set("rl_device_config", config_str_devices)

        if config_str_devices == "":
            config_str_devices = "[]"
            self._rhapi.db.option_set("rl_device_config", config_str_devices)

        config_list_devices = list(eval(config_str_devices))
        rl_devicelist.clear()

        for device in config_list_devices:
            logger.debug(device)
            try:
                flags = device.get("flags", None)
                presetId = device.get("presetId", None)

                if flags is None:
                    legacy_state = int(device.get("state", 1) or 0)
                    flags = RL_FLAG_POWER_ON if legacy_state else 0
                    if "brightness" in device:
                        flags |= RL_FLAG_HAS_BRI

                if presetId is None:
                    presetId = int(device.get("effect", 1) or 1)

                brightness = int(device.get("brightness", 70) or 0)

                dev_type = device.get("dev_type", None)
                if dev_type is None:
                    dev_type = device.get("device_type", None)
                if dev_type is None:
                    dev_type = device.get("caps", device.get("type", 0))

                special_state = build_specials_state(int(dev_type or 0), device)
                rl_devicelist.append(
                    create_device(
                        addr=str(device.get("addr", "")).upper(),
                        dev_type=int(dev_type or 0),
                        name=str(device.get("name", "")),
                        groupId=int(device.get("groupId", 0) or 0),
                        version=int(device.get("version", 0) or 0),
                        caps=int(dev_type or 0),
                        flags=int(flags) & 0xFF,
                        presetId=int(presetId) & 0xFF,
                        brightness=brightness & 0xFF,
                        specials=special_state,
                    )
                )
            except Exception:
                logger.exception("RL: failed to load device entry from DB: %r", device)
                continue

        if config_str_groups is None or config_str_groups == "":
            config_str_groups = str([obj.__dict__ for obj in rl_backup_grouplist])
            self._rhapi.db.option_set("rl_groups_config", config_str_groups)

        config_list_groups = list(eval(config_str_groups))
        rl_grouplist.clear()

        for group in config_list_groups:
            logger.debug(group)
            group_dev_type = group.get("dev_type", group.get("device_type", 0))
            rl_grouplist.append(RL_DeviceGroup(group["name"], group["static_group"], group_dev_type))

        rl_grouplist[:] = [
            g
            for g in rl_grouplist
            if str(getattr(g, "name", "")).strip().lower() not in {"unconfigured", "all wled devices"}
        ]

        if not any(str(getattr(g, "name", "")).strip().lower() == "all wled nodes" for g in rl_grouplist):
            rl_grouplist.append(RL_DeviceGroup("All WLED Nodes", static_group=1, dev_type=0))
        else:
            for g in rl_grouplist:
                if str(getattr(g, "name", "")).strip().lower() == "all wled nodes":
                    g.name = "All WLED Nodes"
                    g.static_group = 1
                    g.dev_type = 0

        self.uiDeviceList = self.createUiDevList()
        self.uiGroupList = self.createUiGroupList()
        self.uiDiscoveryGroupList = self.createUiGroupList(True)
        self.register_settings()
        self.register_quickset_ui()
        self.registerActions()
        self._rhapi.ui.broadcast_ui("settings")
        self._rhapi.ui.broadcast_ui("run")

    def discoverPort(self, args):
        """Initialize communicator via LoRaUSB only. No direct serial here."""
        port = self._rhapi.db.option("psi_comms_port", None)
        try:
            self._transport_hooks_installed = False
            self.lora = LoRaUSB(port=port, on_event=None)
            ok = self.lora.discover_and_open()
            if ok:
                self.lora.start()
                self.ready = True
                self._install_transport_hooks()
                used = self.lora.port or "unknown"
                mac = getattr(self.lora, "ident_mac", None)
                if mac:
                    logger.info("RaceLink Communicator ready on %s with MAC: %s", used, mac)
                    if "manual" in args:
                        self._rhapi.ui.message_notify(self._rhapi.__("RaceLink Communicator ready on {} with MAC: {}").format(used, mac))
                return
            else:
                self.ready = False
                logger.warning("No RaceLink Communicator module discovered or configured")
                if "manual" in args:
                    self._rhapi.ui.message_notify(self._rhapi.__("No RaceLink Communicator module discovered or configured"))
        except Exception as ex:
            self.ready = False
            logger.error("LoRaUSB init failed: %s", ex)
            if "manual" in args:
                self._rhapi.ui.message_notify(self._rhapi.__("Failed to initialize communicator: {}").format(str(ex)))

    def onRaceStart(self, _args):
        logger.warning("RaceLink Race Start Event")

    def onRaceFinish(self, _args):
        logger.warning("RaceLink Race Finish Event")

    def onRaceStop(self, _args):
        logger.warning("RaceLink Race Stop Event")

    def onSendMessage(self, args):
        logger.warning("Event onSendMessage")

    def getDevices(self, groupFilter=255, targetDevice=None, addToGroup=-1):
        return self.device_service.getDevices(groupFilter=groupFilter, targetDevice=targetDevice, addToGroup=addToGroup)

    def getStatus(self, groupFilter=255, targetDevice=None):
        return self.device_service.getStatus(groupFilter=groupFilter, targetDevice=targetDevice)

    def setNodeGroupId(self, targetDevice: RL_Device, forceSet: bool = False, wait_for_ack: bool = True) -> bool:
        return self.device_service.setNodeGroupId(targetDevice, forceSet=forceSet, wait_for_ack=wait_for_ack)

    def forceGroups(self, args=None, sanityCheck: bool = True):
        return self.device_service.forceGroups(args=args, sanityCheck=sanityCheck)

    def sendRaceLink(self, targetDevice, flags=None, presetId=None, brightness=None):
        return self.control_service.sendRaceLink(targetDevice, flags=flags, presetId=presetId, brightness=brightness)

    def sendGroupControl(self, gcGroupId, gcFlags, gcPresetId, gcBrightness):
        return self.control_service.sendGroupControl(gcGroupId, gcFlags, gcPresetId, gcBrightness)

    def sendWledControl(self, *, targetDevice=None, targetGroup=None, params=None):
        return self.control_service.sendWledControl(targetDevice=targetDevice, targetGroup=targetGroup, params=params)

    def sendStartblockConfig(self, *, targetDevice=None, targetGroup=None, params=None):
        return self.control_service.sendStartblockConfig(targetDevice=targetDevice, targetGroup=targetGroup, params=params)

    def get_current_heat_slot_list(self):
        """
        Returns: [(slot_0based, pilot_callsign, racechannel), ...] sorted by slot.
        racechannel ist z.B. "R3" oder "--" wenn Band/Channel nicht gesetzt ist.
        """

        freq = json.loads(self._rhapi.race.frequencyset.frequencies)
        bands = freq["b"]
        channels = freq["c"]
        racechannels = [
            "--" if band is None else f"{band}{channels[i]}"
            for i, band in enumerate(bands)
        ]

        ctx = self._rhapi._racecontext
        rhdata = ctx.rhdata
        race = ctx.race
        heat_nodes = rhdata.get_heatNodes_by_heat(race.current_heat) or []

        callsign_by_slot = {}
        for hn in heat_nodes:
            slot = int(getattr(hn, "node_index"))
            pid = getattr(hn, "pilot_id", None)
            p = rhdata.get_pilot(pid) if pid else None
            callsign_by_slot[slot] = (p.callsign if p else "")

        n = min(len(racechannels), 8)
        return [(i, callsign_by_slot.get(i, ""), racechannels[i]) for i in range(n)]

    def sendStartblockControl(self, *, targetDevice=None, targetGroup=None, params=None):
        return self.control_service.sendStartblockControl(targetDevice=targetDevice, targetGroup=targetGroup, params=params)

    def _normalize_startblock_slot_list(self, slot_list):
        out = []
        for item in (slot_list or []):
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                out.append((int(item[0]), str(item[1] or ""), str(item[2] or "--")))
            elif isinstance(item, dict):
                s = int(item.get("slot", 0))
                cs = str(item.get("callsign", "") or "")
                rc = str(item.get("racechannel", "--") or "--")
                out.append((s, cs, rc))
        return out

    def _send_and_wait_for_reply(
        self,
        recv3: bytes,
        opcode7: int,
        send_fn,
        timeout_s: float = 8.0,
    ) -> tuple[list[dict], bool]:
        return self.transport_service.send_and_wait_for_reply(recv3, opcode7, send_fn, timeout_s=timeout_s)

    def sendConfig(
        self,
        option,
        data0=0,
        data1=0,
        data2=0,
        data3=0,
        recv3=b"\xFF\xFF\xFF",
        wait_for_ack: bool = False,
        timeout_s: float = 6.0,
    ):
        return self.control_service.sendConfig(
            option,
            data0=data0,
            data1=data1,
            data2=data2,
            data3=data3,
            recv3=recv3,
            wait_for_ack=wait_for_ack,
            timeout_s=timeout_s,
        )

    def _apply_config_update(self, dev: RL_Device, option: int, data0: int) -> None:
        bit_map = {
            0x01: 0,
            0x03: 1,
            0x04: 2,
        }
        bit = bit_map.get(int(option))
        if bit is None:
            return
        mask = 1 << bit
        if int(data0):
            dev.configByte = int(dev.configByte) | mask
        else:
            dev.configByte = int(dev.configByte) & (~mask & 0xFF)

    def sendSync(self, ts24, brightness, recv3=b"\xFF\xFF\xFF"):
        return self.control_service.sendSync(ts24, brightness, recv3=recv3)

    def sendStream(
        self,
        payload: bytes,
        groupId: int | None = None,
        device: RL_Device | None = None,
        retries: int = 2,
        timeout_s: float = 8.0,
    ) -> dict[str, int]:
        return self.control_service.sendStream(payload, groupId=groupId, device=device, retries=retries, timeout_s=timeout_s)

    def _wait_rx_window(self, send_fn, collect_pred=None, fail_safe_s: float = 8.0):
        return self.transport_service.wait_rx_window(send_fn, collect_pred=collect_pred, fail_safe_s=fail_safe_s)

    def _opcode_name(self, opcode7: int) -> str:
        try:
            rule = LPA.find_rule(int(opcode7) & 0x7F)
        except Exception:
            rule = None
        if rule and getattr(rule, "name", None):
            return str(rule.name)
        return f"0x{int(opcode7) & 0x7F:02X}"

    def _log_lora_reply(self, ev: dict) -> None:
        try:
            opc = int(ev.get("opc", -1)) & 0x7F
        except Exception:
            return

        sender3_hex = self._to_hex_str(ev.get("sender3")) or "??????"

        if opc == int(LP.OPC_ACK):
            ack_of = ev.get("ack_of")
            ack_status = ev.get("ack_status")
            ack_seq = ev.get("ack_seq")
            if ack_of is None or ack_status is None:
                return
            ack_name = self._opcode_name(int(ack_of))
            logger.debug(
                "ACK from %s: ack_of=%s (%s) status=%s seq=%s",
                sender3_hex,
                int(ack_of),
                ack_name,
                int(ack_status),
                ack_seq,
            )
            return

        if opc == int(LP.OPC_STATUS) and ev.get("reply") == "STATUS_REPLY":
            logger.debug(
                "STATUS from %s: flags=0x%02X cfg=0x%02X preset=%s bri=%s vbat=%s rssi=%s snr=%s host_rssi=%s host_snr=%s",
                sender3_hex,
                int(ev.get("flags", 0) or 0) & 0xFF,
                int(ev.get("configByte", 0) or 0) & 0xFF,
                ev.get("presetId"),
                ev.get("brightness"),
                ev.get("vbat_mV"),
                ev.get("node_rssi"),
                ev.get("node_snr"),
                ev.get("host_rssi"),
                ev.get("host_snr"),
            )
            return

        if opc == int(LP.OPC_DEVICES) and ev.get("reply") == "IDENTIFY_REPLY":
            mac6 = ev.get("mac6")
            mac12 = bytes(mac6).hex().upper() if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6 else None
            dev_type = ev.get("caps")
            dtype_name = get_dev_type_info(dev_type).get("name")
            logger.debug(
                "IDENTIFY from %s: mac=%s group=%s ver=%s dev_type=%s (%s) host_rssi=%s host_snr=%s",
                sender3_hex,
                mac12 or sender3_hex,
                ev.get("groupId"),
                ev.get("version"),
                dev_type,
                dtype_name,
                ev.get("host_rssi"),
                ev.get("host_snr"),
            )
            return

        if ev.get("reply"):
            logger.debug("RX %s from %s (opc=0x%02X)", ev.get("reply"), sender3_hex, opc)

    def _log_rx_window_event(self, ev: dict) -> None:
        t = ev.get("type")
        if getattr(self, "lora", None):
            state = int(ev.get("rx_windows", getattr(self.lora, "rx_window_state", 0)) or 0)
        else:
            state = int(ev.get("rx_windows", 0) or 0)
        if t == EV_RX_WINDOW_OPEN:
            logger.debug("RX window OPEN: state=%s min_ms=%s", state, ev.get("window_ms"))
        elif t == EV_RX_WINDOW_CLOSED:
            logger.debug("RX window CLOSED: state=%s delta=%s", state, ev.get("rx_count_delta"))

    def _handle_ack_event(self, ev: dict) -> None:
        return self.transport_service.handle_ack_event(ev)

    def _install_transport_hooks(self) -> None:
        return self.transport_service.install_hooks()

    def _on_transport_tx(self, ev: dict) -> None:
        return self.transport_service.on_transport_tx(ev)

    def _on_transport_event_gc(self, ev: dict) -> None:
        return self.transport_service.on_transport_event(ev)

    def _schedule_reconnect(self, reason: str) -> None:
        now = time.time()
        if self._reconnect_in_progress or (now - self._last_reconnect_ts) < 5:
            return
        self._last_reconnect_ts = now
        self._reconnect_in_progress = True

        def _reconnect():
            try:
                logger.warning("RaceLink: attempting LoRaUSB reconnect after error: %s", reason)
                try:
                    if self.lora:
                        self.lora.close()
                except Exception:
                    pass
                self.lora = None
                self.discoverPort({})
            finally:
                self._reconnect_in_progress = False

        threading.Thread(target=_reconnect, daemon=True).start()

    def _pending_try_match(self, ev: dict) -> None:
        p = self._pending_expect
        if not p:
            return

        try:
            sender3_hex = self._to_hex_str(ev.get("sender3")).upper()
            if not sender3_hex:
                return
            if sender3_hex != (p.get("sender_last3") or "").upper():
                return

            rule = p.get("rule")
            opcode7 = int(p.get("opcode7", -1)) & 0x7F
            policy = int(getattr(rule, "policy", getattr(LPA, "RESP_NONE", 0)))

            if policy == int(getattr(LPA, "RESP_ACK", 1)):
                if int(ev.get("opc", -1)) == int(LP.OPC_ACK) and int(ev.get("ack_of", -2)) == opcode7:
                    dev = p.get("dev")
                    if dev:
                        dev.mark_online()
                    self._pending_expect = None
            elif policy == int(getattr(LPA, "RESP_SPECIFIC", 2)):
                rsp_opc = int(getattr(rule, "rsp_opcode7", -1)) & 0x7F
                if int(ev.get("opc", -1)) == rsp_opc:
                    dev = p.get("dev")
                    if dev:
                        dev.mark_online()
                    self._pending_expect = None
        except Exception:
            logger.exception("RaceLink: pending match failed")

    def _pending_window_closed(self, ev: dict) -> None:
        p = self._pending_expect
        if not p:
            return

        try:
            dev = p.get("dev")
            rule = p.get("rule")
            opcode7 = int(p.get("opcode7", -1)) & 0x7F
            name = getattr(rule, "name", f"opc=0x{opcode7:02X}")
            if dev:
                dev.mark_offline(f"Missing reply ({name})")
        finally:
            self._pending_expect = None

    def getDeviceFromAddress(self, addr: str) -> Optional[RL_Device]:
        return self.device_service.getDeviceFromAddress(addr)

    @staticmethod
    def _to_hex_str(addr: Union[str, bytes, bytearray, None]) -> str:
        if addr is None:
            return ""
        if isinstance(addr, (bytes, bytearray)):
            return bytes(addr).hex().upper()
        return str(addr).strip().replace(":", "").replace(" ", "").upper()
