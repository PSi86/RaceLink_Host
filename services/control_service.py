from __future__ import annotations

import logging
import time

from ..data import RL_Device, RL_FLAG_HAS_BRI, RL_FLAG_POWER_ON, get_dev_type_info, rl_devicelist

try:
    from ..racelink_transport import LP, _mac_last3_from_hex
except Exception:
    from racelink_transport import LP, _mac_last3_from_hex


logger = logging.getLogger(__name__)


class ControlService:
    def __init__(self, controller):
        self._controller = controller

    def _require_lora(self, context: str):
        if getattr(self._controller, "lora", None):
            return True
        logger.warning("%s: communicator not ready", context)
        return False

    @staticmethod
    def _coerce_control_values(flags, preset_id, brightness, *, fallback: RL_Device | None = None):
        if fallback is not None:
            flags = fallback.flags if flags is None else flags
            preset_id = fallback.presetId if preset_id is None else preset_id
            brightness = fallback.brightness if brightness is None else brightness
        return int(flags) & 0xFF, int(preset_id) & 0xFF, int(brightness) & 0xFF

    @staticmethod
    def _update_group_control_cache(group_id: int, flags: int, preset_id: int, brightness: int) -> None:
        for device in rl_devicelist:
            try:
                if (int(getattr(device, "groupId", 0)) & 0xFF) != group_id:
                    continue
                device.flags = flags
                device.presetId = preset_id
                device.brightness = brightness
            except Exception:
                continue

    def sendRaceLink(self, targetDevice, flags=None, presetId=None, brightness=None):
        if not self._require_lora("sendRaceLink"):
            return
        recv3 = _mac_last3_from_hex(targetDevice.addr)
        groupId = int(targetDevice.groupId) & 0xFF

        f, p, b = self._coerce_control_values(flags, presetId, brightness, fallback=targetDevice)

        self._controller.lora.send_control(recv3=recv3, group_id=groupId, flags=f, preset_id=p, brightness=b)

        targetDevice.flags = f
        targetDevice.presetId = p
        targetDevice.brightness = b

    def sendGroupControl(self, gcGroupId, gcFlags, gcPresetId, gcBrightness):
        if not self._require_lora("sendGroupControl"):
            return

        groupId = int(gcGroupId) & 0xFF
        f, p, b = self._coerce_control_values(gcFlags, gcPresetId, gcBrightness)

        self._update_group_control_cache(groupId, f, p, b)

        self._controller.lora.send_control(
            recv3=b"\xFF\xFF\xFF",
            group_id=groupId,
            flags=f,
            preset_id=p,
            brightness=b,
        )

    def sendWledControl(self, *, targetDevice=None, targetGroup=None, params=None):
        if params is None:
            params = {}
        preset_id = int(params.get("presetId", 1))
        brightness = int(params.get("brightness", 0))
        flags = (RL_FLAG_POWER_ON if brightness > 0 else 0) | RL_FLAG_HAS_BRI

        if targetGroup is not None:
            self.sendGroupControl(int(targetGroup), flags, preset_id, brightness)
            return True
        if targetDevice is not None:
            self.sendRaceLink(targetDevice, flags, preset_id, brightness)
            return True
        return False

    def sendStartblockConfig(self, *, targetDevice=None, targetGroup=None, params=None):
        c = self._controller
        if targetGroup is not None:
            return False
        if not targetDevice or not self._require_lora("sendStartblockConfig"):
            return False
        if params is None:
            params = {}
        slots = int(params.get("startblock_slots", 1))
        first_slot = int(params.get("startblock_first_slot", 1))
        recv3 = _mac_last3_from_hex(targetDevice.addr)
        if not recv3:
            return False
        ok_slots = self.sendConfig(option=0x8C, data0=slots, recv3=recv3, wait_for_ack=True)
        if not ok_slots:
            return False
        ok_first = self.sendConfig(option=0x8D, data0=first_slot, recv3=recv3, wait_for_ack=True)
        if not ok_first:
            return False
        targetDevice.specials["startblock_slots"] = slots & 0xFF
        targetDevice.specials["startblock_first_slot"] = first_slot & 0xFF
        try:
            c.save_to_db({"manual": True})
        except Exception:
            pass
        return True

    def _is_startblock_device(self, dev: RL_Device) -> bool:
        try:
            type_id = getattr(dev, "caps", getattr(dev, "dev_type", 0))
            info = get_dev_type_info(type_id)
            return bool(info.get("STARTBLOCK"))
        except Exception:
            return False

    def _iter_startblock_devices(self, *, targetDevice=None, targetGroup=None) -> list[RL_Device]:
        if targetDevice is not None:
            return [targetDevice] if self._is_startblock_device(targetDevice) else []

        if targetGroup is not None:
            gid = int(targetGroup)
            return [
                dev
                for dev in rl_devicelist
                if self._is_startblock_device(dev) and int(getattr(dev, "groupId", 0) or 0) == gid
            ]

        return [dev for dev in rl_devicelist if self._is_startblock_device(dev)]

    def sendStartblockControl(self, *, targetDevice=None, targetGroup=None, params=None):
        c = self._controller
        if not self._require_lora("sendStartblockControl"):
            return {}
        if params is None:
            params = {}

        use_heat = params.get("startblock_use_current_heat")
        if use_heat is None:
            use_heat = True

        if use_heat:
            slot_list = c.get_current_heat_slot_list()
        else:
            slot_list = params.get("startblock_slot_list") or []
            slot_list = c._normalize_startblock_slot_list(slot_list)

        slot_map = {int(s): (cs or "", rc or "--") for (s, cs, rc) in slot_list}
        slots_0based = [(i, *slot_map.get(i, ("", "--"))) for i in range(8)]

        if targetGroup is not None:
            gid = int(targetGroup)
            sent = []
            for slot0, cs, rc in slots_0based:
                from ..controller import build_startblock_payload_v1
                payload = build_startblock_payload_v1(slot0 + 1, rc, cs)
                sent.append({"slot": slot0 + 1, "result": self.sendStream(payload, groupId=gid)})
            return {"mode": "group", "groupId": gid, "sent": sent}

        if targetDevice is not None:
            if not self._is_startblock_device(targetDevice):
                return {"error": "targetDevice has no STARTBLOCK capability"}
            devices = [targetDevice]
        else:
            devices = self._iter_startblock_devices(targetDevice=None, targetGroup=None)

        if not devices:
            return {"mode": "unicast", "sent": []}

        slot_to_dev = {}
        dev_ranges = []
        for dev in devices:
            try:
                startblock_slots = int(dev.specials["startblock_slots"])
                startblock_first_slot = int(dev.specials["startblock_first_slot"])
            except Exception:
                startblock_slots = 8
                startblock_first_slot = 1

            startblock_slots = max(1, min(8, startblock_slots))
            startblock_first_slot = max(1, min(8, startblock_first_slot))
            last = min(8, startblock_first_slot + startblock_slots - 1)

            dev_ranges.append((dev, startblock_first_slot, last))

            for s in range(startblock_first_slot, last + 1):
                slot_to_dev.setdefault(s, dev)

        sent = []
        for slot0, cs, rc in slots_0based:
            slot1 = slot0 + 1
            dev = slot_to_dev.get(slot1)
            if dev is None:
                continue

            from ..controller import build_startblock_payload_v1
            payload = build_startblock_payload_v1(slot1, rc, cs)
            sent.append({
                "slot": slot1,
                "device": getattr(dev, "deviceId", getattr(dev, "mac", None)),
                "result": self.sendStream(payload, device=dev),
            })

        return {
            "mode": "unicast",
            "devices": [{"device": getattr(d, "deviceId", getattr(d, "mac", None)), "first": a, "last": b} for (d, a, b) in dev_ranges],
            "sent": sent,
        }

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
        c = self._controller
        if not getattr(c, "lora", None):
            logger.warning("sendConfig: communicator not ready")
            return False if wait_for_ack else None
        recv3_hex = recv3.hex().upper() if isinstance(recv3, (bytes, bytearray)) else ""
        dev = None
        if recv3_hex and recv3_hex != "FFFFFF":
            c._pending_config[recv3_hex] = {"option": int(option) & 0xFF, "data0": int(data0) & 0xFF}
            dev = c.getDeviceFromAddress(recv3_hex)
            if dev and wait_for_ack:
                dev.ack_clear()

        def _send():
            c.lora.send_config(
                recv3=recv3,
                option=int(option) & 0xFF,
                data0=int(data0) & 0xFF,
                data1=int(data1) & 0xFF,
                data2=int(data2) & 0xFF,
                data3=int(data3) & 0xFF,
            )

        if wait_for_ack:
            if not dev:
                _send()
                return False
            events, _ = c.transport_service.send_and_wait_for_reply(recv3, LP.OPC_CONFIG, _send, timeout_s=timeout_s)
            if not events:
                return False
            ev = events[-1]
            return bool(int(ev.get("ack_status", 1)) == 0)
        _send()
        return True

    def sendSync(self, ts24, brightness, recv3=b"\xFF\xFF\xFF"):
        if not getattr(self._controller, "lora", None):
            logger.warning("sendSync: communicator not ready")
            return
        self._controller.lora.send_sync(recv3=recv3, ts24=int(ts24) & 0xFFFFFF, brightness=int(brightness) & 0xFF)

    @staticmethod
    def _stream_ctrl(start: bool, stop: bool, packets_left: int) -> int:
        ctrl = (0x80 if start else 0x00) | (0x40 if stop else 0x00)
        return ctrl | (int(packets_left) & 0x3F)

    def sendStream(self, payload: bytes, groupId: int | None = None, device: RL_Device | None = None, retries: int = 2, timeout_s: float = 8.0) -> dict[str, int]:
        c = self._controller
        if not getattr(c, "lora", None):
            logger.warning("sendStream: communicator not ready")
            return {}

        c.transport_service.install_hooks()

        data = bytes(payload or b"")
        if len(data) > 128:
            raise ValueError("payload too large (max 128 bytes)")

        if device is None and groupId is None:
            raise ValueError("sendStream requires groupId or device")

        total_packets = max(1, (len(data) + 7) // 8)
        start = True
        stop = total_packets == 1
        packets_left = 0 if stop else total_packets
        ctrl = self._stream_ctrl(start, stop, packets_left)

        if device is None:
            targets = [dev for dev in rl_devicelist if int(getattr(dev, "groupId", 0) or 0) == int(groupId)]
        else:
            targets = [device]

        target_last3 = {_mac_last3_from_hex(dev.addr) for dev in targets if dev and dev.addr}
        target_last3.discard(b"\xFF\xFF\xFF")
        expected = len(target_last3)
        if expected == 0:
            return {"expected": 0, "acked": 0}

        recv3 = b"\xFF\xFF\xFF" if device is None else _mac_last3_from_hex(device.addr)
        if recv3 == b"\xFF\xFF\xFF" and device is not None:
            return {"expected": expected, "acked": 0}

        try:
            c.lora.drain_events(0.0)
        except Exception:
            pass

        acked = set()

        def _collect(ev: dict) -> bool:
            try:
                if ev.get("opc") != LP.OPC_ACK:
                    return False
                if int(ev.get("ack_of", -1)) != int(LP.OPC_STREAM):
                    return False
                sender3 = ev.get("sender3")
                if not isinstance(sender3, (bytes, bytearray)):
                    return False
                sender3_b = bytes(sender3)
                if sender3_b not in target_last3:
                    return False
                acked.add(sender3_b)
                return True
            except Exception:
                return False

        for attempt in range(max(0, int(retries)) + 1):
            c.transport_service.wait_rx_window(
                lambda: c.lora.send_stream(recv3=recv3, ctrl=ctrl, data=data),
                collect_pred=_collect,
                fail_safe_s=timeout_s,
            )
            if len(acked) >= expected:
                break
            if attempt < int(retries):
                time.sleep(0.1)

        return {"expected": expected, "acked": len(acked)}
