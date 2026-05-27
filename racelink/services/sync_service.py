"""Sync command service for RaceLink devices.

Thin wrapper around :meth:`GatewayService.send_sync` that fires
an OPC_SYNC broadcast packet. The packet's ``ts24`` field is the
gateway-relative 24-bit timestamp; the device adjusts its timebase
on every SYNC. Pending arm-on-sync state materialises ONLY when the
``trigger_armed`` kwarg is set, which writes ``SYNC_FLAG_TRIGGER_ARMED``
into the wire body. See ``racelink_proto.h`` for the materialisation
rules.

Public API:

* ``send_sync(ts24, brightness, recv3=b"\\xFF\\xFF\\xFF", trigger_armed=False, target=None)``
  — defaults to broadcast, clock-tick only. The scene runner's
  ``_run_sync`` (and any operator-driven manual fire) MUST pass
  ``trigger_armed=True`` to fire armed effects; autosync MUST leave it
  ``False`` so the autosync packet cannot accidentally fire armed
  state ahead of a deliberate sync. The ``target`` kwarg is the
  explicit set of networks the broadcast addresses (see
  :class:`BroadcastTarget`); when omitted, ``GatewayService.send_sync``
  falls back to the deprecated "all attached" behaviour with a
  warning.

Threading: scene runner calls this synchronously from its
dispatcher thread. Broadcast fan-out across multiple transports runs
on worker threads (via :func:`broadcast_fanout`); this method
returns once every worker's ``EV_TX_DONE`` has landed (or timed out).
"""

from __future__ import annotations

import logging
from typing import Optional

from ..transport.broadcast_target import BroadcastTarget

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(self, controller, gateway_service):
        self.controller = controller
        self.gateway_service = gateway_service

    @property
    def transport(self):
        return getattr(self.controller, "transport", None)

    def send_sync(self, ts24, brightness, recv3=b"\xFF\xFF\xFF", *,
                  trigger_armed: bool = False,
                  target: Optional[BroadcastTarget] = None):
        # Readiness check: refuse to send when no transport is attached
        # at all. Multi-network deployments where one transport is
        # mid-reconnect still get a tick on the remaining radios — the
        # gateway-service layer's per-transport routing skips the
        # missing network and logs a warning.
        transports = list(getattr(self.controller, "transports", None) or [])
        if not transports and self.transport is None:
            logger.warning("sendSync: communicator not ready")
            return
        self.gateway_service.send_sync(
            ts24, brightness,
            recv3=recv3,
            trigger_armed=trigger_armed,
            target=target,
        )
