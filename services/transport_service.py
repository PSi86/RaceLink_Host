from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from ..data import create_device, rl_devicelist

try:
    from .. import lora_proto_auto as LPA
except Exception:
    import lora_proto_auto as LPA

try:
    from ..racelink_transport import LP, EV_ERROR, EV_RX_WINDOW_CLOSED, EV_RX_WINDOW_OPEN
except Exception:
    from racelink_transport import LP, EV_ERROR, EV_RX_WINDOW_CLOSED, EV_RX_WINDOW_OPEN

if TYPE_CHECKING:
    from ..controller import RaceLink_LoRa

logger = logging.getLogger(__name__)


class TransportService:
    def __init__(self, controller: "RaceLink_LoRa") -> None:
        self._controller = controller

    def wait_rx_window(self, send_fn, collect_pred=None, fail_safe_s: float = 8.0):
        lora = getattr(self._controller, "lora", None)
        if not lora:
            return [], False

        collected = []
        got_closed = False

        if hasattr(lora, "add_listener") and hasattr(lora, "remove_listener"):
            closed_ev = threading.Event()

            def _cb(ev: dict):
                nonlocal got_closed
                try:
                    if not isinstance(ev, dict):
                        return
                    if ev.get("type") == EV_RX_WINDOW_CLOSED:
                        got_closed = True
                        closed_ev.set()
                        return
                    if collect_pred and collect_pred(ev):
                        collected.append(ev)
                except Exception:
                    pass

            lora.add_listener(_cb)
            try:
                send_fn()
                closed_ev.wait(timeout=float(fail_safe_s))
            finally:
                try:
                    lora.remove_listener(_cb)
                except Exception:
                    pass
            return collected, got_closed

        send_fn()
        t_end = time.time() + float(fail_safe_s)
        while time.time() < t_end:
            for ev in lora.drain_events(timeout_s=0.1):
                if ev.get("type") == EV_RX_WINDOW_CLOSED:
                    got_closed = True
                    return collected, got_closed
                if collect_pred and collect_pred(ev):
                    collected.append(ev)
        return collected, got_closed

    def send_and_wait_for_reply(self, recv3: bytes, opcode7: int, send_fn, timeout_s: float = 8.0):
        if not getattr(self._controller, "lora", None):
            return [], False

        self.install_hooks()

        opcode7 = int(opcode7) & 0x7F
        recv3_b = bytes(recv3 or b"")
        sender_filter = recv3_b if recv3_b and recv3_b != b"\xFF\xFF\xFF" else None
        sender_filter_hex = sender_filter.hex().upper() if sender_filter else ""
        sender_dev = self._controller.getDeviceFromAddress(sender_filter_hex) if sender_filter_hex else None

        try:
            rule = LPA.find_rule(opcode7)
        except Exception:
            rule = None

        policy = int(getattr(rule, "policy", getattr(LPA, "RESP_NONE", 0))) if rule else int(getattr(LPA, "RESP_NONE", 0))
        if policy == int(getattr(LPA, "RESP_NONE", 0)):
            send_fn()
            return [], False

        rsp_opc = int(getattr(rule, "rsp_opcode7", -1)) & 0x7F if rule else -1

        def _collect(ev: dict) -> bool:
            try:
                sender3 = ev.get("sender3")
                if sender_filter is not None:
                    if not isinstance(sender3, (bytes, bytearray)):
                        return False
                    if bytes(sender3) != sender_filter:
                        return False

                opc = int(ev.get("opc", -1))
                if policy == int(getattr(LPA, "RESP_ACK", 1)):
                    if opc == int(LP.OPC_ACK) and int(ev.get("ack_of", -1)) == opcode7:
                        if sender_dev:
                            sender_dev.mark_online()
                        return True
                elif policy == int(getattr(LPA, "RESP_SPECIFIC", 2)):
                    if opc == rsp_opc:
                        if sender_dev:
                            sender_dev.mark_online()
                        return True
            except Exception:
                return False
            return False

        return self.wait_rx_window(send_fn, collect_pred=_collect, fail_safe_s=timeout_s)

    def handle_ack_event(self, ev: dict) -> None:
        try:
            sender3_hex = self._controller._to_hex_str(ev.get("sender3"))
            dev = self._controller.getDeviceFromAddress(sender3_hex) if sender3_hex else None
            if not dev:
                return

            ack_of = ev.get("ack_of")
            ack_status = ev.get("ack_status")
            ack_seq = ev.get("ack_seq")
            host_rssi = ev.get("host_rssi")
            host_snr = ev.get("host_snr")

            if ack_of is None or ack_status is None:
                return

            dev.ack_update(int(ack_of), int(ack_status), ack_seq, host_rssi, host_snr)

            if int(ack_of) == int(LP.OPC_CONFIG) and int(ack_status) == 0:
                pending = self._controller._pending_config.pop(sender3_hex, None)
                if pending:
                    self._controller._apply_config_update(dev, pending.get("option", 0), pending.get("data0", 0))

        except Exception:
            logger.exception("ACK handling failed")

    def install_hooks(self) -> None:
        if self._controller._transport_hooks_installed:
            return
        lora = getattr(self._controller, "lora", None)
        if not lora:
            return

        try:
            if hasattr(lora, "add_listener"):
                lora.add_listener(self.on_transport_event)
            else:
                prev = getattr(lora, "on_event", None)

                def _mux(ev):
                    try:
                        self.on_transport_event(ev)
                    except Exception:
                        pass
                    if prev:
                        try:
                            prev(ev)
                        except Exception:
                            pass

                lora.on_event = _mux
        except Exception:
            logger.exception("RaceLink: failed to install transport RX listener")

        try:
            if hasattr(lora, "add_tx_listener"):
                lora.add_tx_listener(self.on_transport_tx)
        except Exception:
            logger.exception("RaceLink: failed to install transport TX listener")

        self._controller._transport_hooks_installed = True

    def on_transport_tx(self, ev: dict) -> None:
        try:
            if not ev or ev.get("type") != "TX_M2N":
                return
            recv3 = ev.get("recv3")
            if not isinstance(recv3, (bytes, bytearray)) or len(recv3) != 3:
                return
            recv3_b = bytes(recv3)

            if recv3_b == b"\xFF\xFF\xFF":
                return

            opcode7 = int(ev.get("opc", -1)) & 0x7F
            try:
                rule = LPA.find_rule(opcode7)
            except Exception:
                rule = None
            if not rule:
                return

            if int(getattr(rule, "req_dir", getattr(LPA, "DIR_M2N", 0))) != int(getattr(LPA, "DIR_M2N", 0)):
                return

            policy = int(getattr(rule, "policy", getattr(LPA, "RESP_NONE", 0)))
            if policy == int(getattr(LPA, "RESP_NONE", 0)):
                return

            dev = self._controller.getDeviceFromAddress(recv3_b.hex().upper())
            if not dev:
                return

            self._controller._pending_expect = {
                "dev": dev,
                "rule": rule,
                "opcode7": opcode7,
                "sender_last3": (dev.addr or "").upper()[-6:],
                "ts": time.time(),
            }
        except Exception:
            logger.exception("RaceLink: TX hook failed")

    def on_transport_event(self, ev: dict) -> None:
        c = self._controller
        try:
            if not isinstance(ev, dict):
                return

            t = ev.get("type")

            if t == EV_ERROR:
                reason = str(ev.get("data") or "unknown error")
                c.ready = False
                now = time.time()
                if (now - c._last_error_notify_ts) > 2:
                    c._last_error_notify_ts = now
                    try:
                        c._rhapi.ui.message_notify(c._rhapi.__("RaceLink Communicator disconnected: {}").format(reason))
                    except Exception:
                        logger.exception("RaceLink: failed to notify UI about disconnect")
                c._schedule_reconnect(reason)
                return

            if t in (EV_RX_WINDOW_OPEN, EV_RX_WINDOW_CLOSED):
                c._log_rx_window_event(ev)
                if t == EV_RX_WINDOW_CLOSED:
                    c._pending_window_closed(ev)
                return

            opc = ev.get("opc")
            if opc is None:
                return

            c._log_lora_reply(ev)

            if int(opc) == int(LP.OPC_ACK):
                self.handle_ack_event(ev)
            elif int(opc) == int(LP.OPC_STATUS) and ev.get("reply") == "STATUS_REPLY":
                sender3_hex = c._to_hex_str(ev.get("sender3"))
                dev = c.getDeviceFromAddress(sender3_hex) if sender3_hex else None
                if dev:
                    dev.update_from_status(
                        ev.get("flags"),
                        ev.get("configByte"),
                        ev.get("presetId"),
                        ev.get("brightness"),
                        ev.get("vbat_mV"),
                        ev.get("node_rssi"),
                        ev.get("node_snr"),
                        ev.get("host_rssi"),
                        ev.get("host_snr"),
                    )
            elif int(opc) == int(LP.OPC_DEVICES) and ev.get("reply") == "IDENTIFY_REPLY":
                mac6 = ev.get("mac6")
                if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                    mac12 = bytes(mac6).hex().upper()
                    dev = c.getDeviceFromAddress(mac12)
                    if not dev:
                        dev_type = ev.get("caps", 0)
                        dev = create_device(addr=mac12, dev_type=int(dev_type or 0), name=f"WLED {mac12}")
                        rl_devicelist.append(dev)
                        try:
                            if hasattr(c, "createUiDevList"):
                                c.uiDeviceList = c.createUiDevList()
                        except Exception:
                            pass

                    dev.update_from_identify(
                        ev.get("version"),
                        ev.get("caps"),
                        ev.get("groupId"),
                        mac6,
                        ev.get("host_rssi"),
                        ev.get("host_snr"),
                    )

            c._pending_try_match(ev)

        except Exception:
            logger.exception("RaceLink: RX hook failed")
