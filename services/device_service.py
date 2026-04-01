from __future__ import annotations

import logging

from ..data import RL_Device, rl_devicelist, rl_grouplist

try:
    from ..racelink_transport import LP, _mac_last3_from_hex
except Exception:
    from racelink_transport import LP, _mac_last3_from_hex

logger = logging.getLogger(__name__)


class DeviceService:
    def __init__(self, controller):
        self._controller = controller

    def getDevices(self, groupFilter=255, targetDevice=None, addToGroup=-1):
        c = self._controller
        if not getattr(c, "lora", None):
            logger.warning("getDevices: communicator not ready")
            return 0

        c.transport_service.install_hooks()

        if targetDevice is None:
            recv3 = b"\xFF\xFF\xFF"
            groupId = int(groupFilter) & 0xFF
        else:
            recv3 = _mac_last3_from_hex(targetDevice.addr)
            groupId = int(targetDevice.groupId) & 0xFF

        found = 0
        responders = set()

        def _collect(ev: dict) -> bool:
            nonlocal found
            try:
                if ev.get("opc") == LP.OPC_DEVICES and ev.get("reply") == "IDENTIFY_REPLY":
                    found += 1
                    mac6 = ev.get("mac6")
                    if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                        responders.add(bytes(mac6).hex().upper())
                    else:
                        sender3 = ev.get("sender3")
                        sender_hex = c._to_hex_str(sender3)
                        if sender_hex:
                            responders.add(sender_hex.upper())
                    return True
            except Exception:
                pass
            return False

        logger.debug("GET_DEVICES -> recv3=%s group=%d flags=%d", recv3.hex().upper(), groupId, 0)

        try:
            c.lora.drain_events(0.0)
        except Exception:
            pass

        c.transport_service.wait_rx_window(
            lambda: c.lora.send_get_devices(recv3=recv3, group_id=groupId, flags=0),
            collect_pred=_collect,
            fail_safe_s=8.0,
        )

        if addToGroup > 0 and addToGroup < 255:
            for addr in responders:
                dev = self.getDeviceFromAddress(addr)
                if not dev:
                    continue
                dev.groupId = addToGroup
                self.setNodeGroupId(dev)

        if hasattr(c, "_rhapi") and hasattr(c._rhapi, "ui"):
            if addToGroup > 0 and addToGroup < 255:
                msg = "Device Discovery finished with {} devices found and added to GroupId: {}".format(found, addToGroup)
            else:
                msg = "Device Discovery finished with {} devices found.".format(found)
            c._rhapi.ui.message_notify(msg)
        return found

    def getStatus(self, groupFilter=255, targetDevice=None):
        c = self._controller
        if not getattr(c, "lora", None):
            logger.warning("getStatus: communicator not ready")
            return 0

        c.transport_service.install_hooks()

        if targetDevice is None:
            recv3 = b"\xFF\xFF\xFF"
            groupId = int(groupFilter) & 0xFF
            sender_filter = None
        else:
            recv3 = _mac_last3_from_hex(targetDevice.addr)
            groupId = int(targetDevice.groupId) & 0xFF
            sender_filter = recv3.hex().upper()

        updated = 0
        responders = set()

        def _collect(ev: dict) -> bool:
            nonlocal updated
            try:
                if ev.get("opc") == LP.OPC_STATUS and ev.get("reply") == "STATUS_REPLY":
                    if sender_filter:
                        sender3 = ev.get("sender3")
                        if isinstance(sender3, (bytes, bytearray)) and bytes(sender3).hex().upper() != sender_filter:
                            return False
                    updated += 1
                    try:
                        mac6 = ev.get("mac6")
                        if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                            responders.add(bytes(mac6).hex().upper())
                        else:
                            sender3 = ev.get("sender3")
                            if isinstance(sender3, (bytes, bytearray)) and len(sender3) == 3:
                                responders.add(bytes(sender3).hex().upper())
                    except Exception:
                        pass
                    return True
            except Exception:
                pass
            return False

        try:
            c.lora.drain_events(0.0)
        except Exception:
            pass

        _, got_closed = c.transport_service.wait_rx_window(
            lambda: c.lora.send_get_status(recv3=recv3, group_id=groupId, flags=0),
            collect_pred=_collect,
            fail_safe_s=8.0,
        )

        if got_closed:
            if targetDevice is not None:
                if updated == 0:
                    try:
                        targetDevice.mark_offline("Missing reply (STATUS)")
                    except Exception:
                        pass
            else:
                if groupFilter == 255:
                    targets = list(rl_devicelist)
                else:
                    targets = [dev for dev in rl_devicelist if int(getattr(dev, "groupId", 0)) == int(groupFilter)]
                for dev in targets:
                    try:
                        mac = (dev.addr or "").upper()
                        if not mac:
                            continue
                        if mac not in responders and mac[-6:] not in responders:
                            dev.mark_offline("Missing reply (STATUS)")
                    except Exception:
                        pass

        return updated

    def setNodeGroupId(self, targetDevice: RL_Device, forceSet: bool = False, wait_for_ack: bool = True) -> bool:
        c = self._controller
        if not getattr(c, "lora", None):
            logger.warning("setNodeGroupId: communicator not ready")
            return False

        c.transport_service.install_hooks()

        recv3 = _mac_last3_from_hex(targetDevice.addr)
        group_id = int(targetDevice.groupId) & 0xFF
        is_broadcast = recv3 == b"\xFF\xFF\xFF"

        if not is_broadcast:
            targetDevice.ack_clear()

        def _send():
            c.lora.send_set_group(recv3, group_id)

        if not wait_for_ack or is_broadcast:
            _send()
            return True

        events, _ = c.transport_service.send_and_wait_for_reply(recv3, LP.OPC_SET_GROUP, _send, timeout_s=8.0)
        if not events:
            logger.warning("No ACK_OK for SET_GROUP to %s (timeout)", targetDevice.addr)
            return False

        ev = events[-1]
        ok = int(ev.get("ack_status", 1)) == 0
        if not ok:
            logger.warning(
                "No ACK_OK for SET_GROUP to %s (status=%s, opcode=%s)",
                targetDevice.addr,
                ev.get("ack_status"),
                ev.get("ack_of"),
            )
        return ok

    def forceGroups(self, args=None, sanityCheck: bool = True):
        logger.debug("Forcing all known devices to their stored groups.")
        num_groups = len(rl_grouplist)

        for device in rl_devicelist:
            if sanityCheck is True and device.groupId >= num_groups:
                device.groupId = 0
            self.setNodeGroupId(device, forceSet=True)

    @staticmethod
    def getDeviceFromAddress(addr: str):
        if not addr:
            return None
        s = str(addr).strip().upper()
        if len(s) == 12:
            for d in rl_devicelist:
                if (d.addr or "").upper() == s:
                    return d
            return None
        if len(s) == 6:
            for d in rl_devicelist:
                if (d.addr or "").upper().endswith(s):
                    return d
            return None
        return None
