from __future__ import annotations

import time
from typing import Optional


class SseState:
    def __init__(self, clients_lock, task_lock):
        self._clients_lock = clients_lock
        self._task_lock = task_lock
        self._clients = set()
        self._master = {
            "state": "IDLE",
            "tx_pending": False,
            "rx_window_open": False,
            "rx_windows": 0,
            "rx_window_ms": 0,
            "last_event": None,
            "last_event_ts": 0.0,
            "last_tx_len": 0,
            "last_rx_count_delta": 0,
            "last_error": None,
        }
        self._task: Optional[dict] = None

    def master_snapshot(self):
        return dict(self._master)

    def task_snapshot(self):
        with self._task_lock:
            return dict(self._task) if self._task else None

    def add_client(self, q):
        with self._clients_lock:
            self._clients.add(q)

    def remove_client(self, q):
        with self._clients_lock:
            self._clients.discard(q)

    def has_clients(self) -> bool:
        with self._clients_lock:
            return bool(self._clients)

    def broadcast(self, ev_name: str, payload):
        with self._clients_lock:
            dead = []
            for q in list(self._clients):
                try:
                    q.put((ev_name, payload), timeout=0.01)
                except Exception:
                    dead.append(q)
            for q in dead:
                self._clients.discard(q)

    def set_master(self, **updates):
        changed = False
        for k, v in updates.items():
            if self._master.get(k) != v:
                self._master[k] = v
                changed = True
        if changed:
            self._master["last_event_ts"] = time.time()
            self.broadcast("master", self.master_snapshot())

    def set_task(self, new_task: Optional[dict]):
        with self._task_lock:
            self._task = new_task
        self.broadcast("task", self.task_snapshot())

    def task_update(self, **updates):
        with self._task_lock:
            if not self._task:
                return
            for k, v in updates.items():
                self._task[k] = v
        self.broadcast("task", self.task_snapshot())

    def task_is_running(self) -> bool:
        with self._task_lock:
            return bool(self._task and self._task.get("state") == "running")
