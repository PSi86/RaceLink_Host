from __future__ import annotations


class TransportHooks:
    def __init__(self, rl_instance, state, log, event_constants):
        self._rl_instance = rl_instance
        self._state = state
        self._log = log
        self._hooked = False
        self.EV_ERROR = event_constants["EV_ERROR"]
        self.EV_RX_WINDOW_OPEN = event_constants["EV_RX_WINDOW_OPEN"]
        self.EV_RX_WINDOW_CLOSED = event_constants["EV_RX_WINDOW_CLOSED"]
        self.EV_TX_DONE = event_constants["EV_TX_DONE"]

    def ensure_hooked(self):
        if self._hooked:
            return
        lora = getattr(self._rl_instance, "lora", None)
        if not lora:
            return

        if hasattr(lora, "add_listener"):
            try:
                lora.add_listener(self.on_transport_event)
                self._hooked = True
                self._log("RaceLink: transport event listener installed (add_listener)")
                return
            except Exception as ex:
                self._log(f"RaceLink: add_listener failed, falling back to on_event: {ex}")

        if not hasattr(lora, "on_event"):
            return

        prev = getattr(lora, "on_event", None)

        def _mux(ev: dict):
            try:
                self.on_transport_event(ev)
            except Exception:
                pass
            try:
                if prev and prev is not _mux:
                    prev(ev)
            except Exception:
                pass

        try:
            lora.on_event = _mux
            self._hooked = True
            self._log("RaceLink: transport event hook installed")
        except Exception as ex:
            self._log(f"RaceLink: transport hook failed: {ex}")

    def on_transport_event(self, ev: dict):
        t = ev.get("type")

        if t == self.EV_RX_WINDOW_OPEN:
            rx_state = int(ev.get("rx_windows", 1) or 0)
            rx_open = rx_state == 1
            master = self._state.master_snapshot()
            self._state.set_master(
                state="RX" if rx_open else ("TX" if master.get("tx_pending") else "IDLE"),
                rx_windows=rx_state,
                rx_window_open=rx_open,
                rx_window_ms=int(ev.get("window_ms", 0) or 0),
                last_event="RX_WINDOW_OPEN",
                last_error=None,
            )
            if self._state.task_is_running():
                snap = self._state.task_snapshot() or {}
                self._state.task_update(rx_window_events=int(snap.get("rx_window_events", 0)) + 1)
            return

        if t == self.EV_RX_WINDOW_CLOSED:
            delta = int(ev.get("rx_count_delta", 0) or 0)
            rx_state = int(ev.get("rx_windows", 0) or 0)
            rx_open = rx_state == 1
            master = self._state.master_snapshot()
            self._state.set_master(
                state="RX" if rx_open else ("TX" if master.get("tx_pending") else "IDLE"),
                rx_windows=rx_state,
                rx_window_open=rx_open,
                rx_window_ms=0,
                last_event="RX_WINDOW_CLOSED",
                last_rx_count_delta=delta,
                last_error=None,
            )
            if self._state.task_is_running():
                snap = self._state.task_snapshot() or {}
                self._state.task_update(
                    rx_count_delta_total=int(snap.get("rx_count_delta_total", 0)) + delta,
                    rx_window_events=int(snap.get("rx_window_events", 0)) + 1,
                )
            return

        if t == self.EV_TX_DONE:
            self._state.set_master(
                tx_pending=False,
                state="RX" if self._state.master_snapshot().get("rx_window_open") else "IDLE",
                last_event="TX_DONE",
                last_tx_len=int(ev.get("last_len", 0) or 0),
                last_error=None,
            )
            return

        if t == self.EV_ERROR:
            raw = ev.get("data", b"")
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.hex().upper()
            self._state.set_master(state="ERROR", last_event="USB_ERROR", last_error=str(raw))
            if self._state.task_is_running():
                self._state.task_update(last_error=str(raw))
            return

        reply = ev.get("reply")
        if not reply:
            return

        if reply == "ACK" and self._state.has_clients():
            self._state.broadcast("refresh", {"what": ["devices"]})

        if self._state.task_is_running():
            snap = self._state.task_snapshot() or {}
            tname = snap.get("name")
            if tname == "discover" and reply == "IDENTIFY_REPLY":
                self._state.task_update(rx_replies=int(snap.get("rx_replies", 0)) + 1)
            elif tname == "status" and reply == "STATUS_REPLY":
                self._state.task_update(rx_replies=int(snap.get("rx_replies", 0)) + 1)

        self._state.set_master(last_event=reply, last_error=None)
