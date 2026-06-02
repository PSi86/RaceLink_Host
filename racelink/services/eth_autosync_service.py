"""Host-side periodic SYNC for Ethernet networks.

On an RF network the LoRa gateway emits the periodic ``OPC_SYNC`` that keeps
every node's timebase aligned — the host never has to. An Ethernet network
has no gateway (the host NIC *is* the transport), so nobody drives that
periodic tick unless the host does it. This service fires an ``OPC_SYNC``
broadcast at a fixed interval (default 30 s) to every attached Ethernet
network, leaving RF networks to their own gateways.

Autosync form (matches the firmware contract):

* ``trigger_armed=False`` — only the device timebase is adjusted; pending
  arm-on-sync state must NOT materialise on an autosync pulse (that is the
  deliberate scene/operator SYNC's job).
* ``brightness=0`` — ignored by the firmware on a non-armed SYNC, so the
  pulse never changes brightness.
* ``ts24`` — the wall-clock-derived 24-bit millisecond timestamp the firmware
  unwraps into its monotonic master timebase. Deliberately the SAME clock the
  scene runner's SYNC uses (``int(time.time() * 1000)``) so an autosync pulse
  and a scene sync never make a device's timebase jump between sources.

Threading: one daemon thread loops on ``threading.Event.wait(interval)`` so
``stop()`` returns promptly. The send fans out across the targeted Ethernet
transports via :meth:`SyncService.send_sync` (which uses the broadcast
fan-out under the hood). The thread is started from ``Controller.onStartup``
and stopped from ``Controller.shutdown``; ``tick()`` is a no-op whenever no
Ethernet network is attached, so the loop is harmless on RF-only deployments.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional

from ..transport.broadcast_target import BroadcastTarget

logger = logging.getLogger(__name__)

#: Default seconds between host-driven SYNC pulses on Ethernet networks.
DEFAULT_ETH_AUTOSYNC_INTERVAL_S = 30.0


class EthAutosyncService:
    def __init__(
        self,
        controller,
        *,
        interval_s: float = DEFAULT_ETH_AUTOSYNC_INTERVAL_S,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ):
        self.controller = controller
        self.interval_s = max(1.0, float(interval_s))
        self._clock_ms = clock_ms
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="rl-eth-autosync", daemon=True,
        )
        self._thread.start()
        logger.info("eth autosync started (interval=%.1fs)", self.interval_s)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None

    # ---- core ------------------------------------------------------------

    def _eth_network_ids(self) -> List[str]:
        """The bound ``network_id`` of every attached Ethernet transport."""
        ids: List[str] = []
        seen: set = set()
        for t in (getattr(self.controller, "transports", None) or ()):
            if getattr(t, "kind", "rf") != "ethernet":
                continue
            nid = str(getattr(t, "network_id", "") or "")
            if nid and nid not in seen:
                seen.add(nid)
                ids.append(nid)
        return ids

    def tick(self) -> int:
        """Fire one autosync pulse to every attached Ethernet network.

        Returns the number of networks addressed (``0`` = nothing to do,
        e.g. an RF-only deployment or no transport ready yet).
        """
        eth_ids = self._eth_network_ids()
        if not eth_ids:
            return 0
        sync_service = getattr(self.controller, "sync_service", None)
        if sync_service is None:
            return 0
        ts24 = int(self._clock_ms()) & 0xFFFFFF
        try:
            sync_service.send_sync(
                ts24, 0,
                trigger_armed=False,
                target=BroadcastTarget.from_ids(eth_ids),
            )
        except Exception:
            logger.exception("eth autosync: send_sync raised")
            return 0
        logger.debug(
            "eth autosync: pulsed %d Ethernet network(s) ts24=%d",
            len(eth_ids), ts24,
        )
        return len(eth_ids)

    def _loop(self) -> None:
        # Wait first so we don't pulse mid-boot while transports are still
        # attaching; then tick once per interval until stopped.
        while not self._stop.wait(self.interval_s):
            try:
                self.tick()
            except Exception:
                # swallow-ok: a single failed tick must not kill the loop;
                # the next interval retries.
                logger.exception("eth autosync loop: tick raised")
