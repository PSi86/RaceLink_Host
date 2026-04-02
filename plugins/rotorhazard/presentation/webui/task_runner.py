from __future__ import annotations

import threading
import time
from typing import Optional


class TaskRunner:
    def __init__(self, state):
        self._state = state
        self._task_lock = threading.Lock()
        self._task_seq = 0

    def start_task(self, name: str, target_fn, meta: Optional[dict] = None):
        with self._task_lock:
            if self._state.task_is_running():
                return None
            self._task_seq += 1
            task = {
                "id": self._task_seq,
                "name": name,
                "state": "running",
                "started_ts": time.time(),
                "ended_ts": None,
                "meta": meta or {},
                "rx_replies": 0,
                "rx_window_events": 0,
                "rx_count_delta_total": 0,
                "last_error": None,
                "result": None,
            }
            self._state.set_task(task)

        def runner():
            try:
                self._state.set_master(state="TX", tx_pending=True, last_event=f"TASK_{name.upper()}_START")
                res = target_fn()
                self._state.task_update(state="done", ended_ts=time.time(), result=res)
                self._state.set_master(
                    state="IDLE" if not self._state.master_snapshot().get("rx_window_open") else "RX",
                    last_event=f"TASK_{name.upper()}_DONE",
                )
                self._state.broadcast("refresh", {"what": ["groups", "devices"]})
            except Exception as ex:
                self._state.task_update(state="error", ended_ts=time.time(), last_error=str(ex))
                self._state.set_master(state="ERROR", last_event=f"TASK_{name.upper()}_ERROR", last_error=str(ex))

        threading.Thread(target=runner, daemon=True).start()
        return self._state.task_snapshot()
