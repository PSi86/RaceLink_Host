"""Uniform per-network reconnect tracker (Round 3 follow-up to the
multi-transport listener-install fix).

Detects "expected but not currently attached" gateways by comparing
``RL_Network.gateway_mac`` against the set of attached transport
``ident_mac``s. While the difference is non-empty, polls
``controller.soft_rediscover()`` every ``poll_interval_s`` seconds.
Stops cleanly when the missing set is empty OR the operator cancels
specific MACs.

State machine (per ident_mac):

    expected  -- gateway_mac persisted on some RL_Network
    attached  -- in controller._transports
    cancelled -- operator-suppressed via /api/gateway/cancel-reconnect

A MAC is "missing" iff (expected AND NOT attached AND NOT cancelled).

SSE: emits ``gateway_missing`` on every state change with the current
missing list + countdown to next poll.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class MissingTransportTracker:
    """Background poll for known-but-detached gateways."""

    EVENT_MISSING = "gateway_missing"

    def __init__(
        self,
        controller,
        *,
        poll_interval_s: float = 5.0,
        broadcast: Optional[Callable[[str, dict], None]] = None,
    ):
        self.controller = controller
        self._poll_interval_s = float(poll_interval_s)
        self._broadcast = broadcast
        self._lock = threading.RLock()
        self._timer: Optional[threading.Timer] = None
        self._cancelled_macs: set[str] = set()
        self._next_fire_ts: Optional[float] = None
        self._shutdown = False

    # ---- wiring -------------------------------------------------------

    def attach_broadcast(self, broadcast: Callable[[str, dict], None]) -> None:
        self._broadcast = broadcast

    # ---- read-only state ----------------------------------------------

    def expected_macs(self) -> set[str]:
        repo = getattr(self.controller, "network_repository", None)
        if repo is None:
            return set()
        try:
            nets = list(repo.list())
        except Exception:
            logger.debug(
                "MissingTransportTracker: network_repository.list raised",
                exc_info=True,
            )
            return set()
        out: set[str] = set()
        for n in nets:
            mac = getattr(n, "gateway_mac", None)
            if mac:
                out.add(str(mac).upper())
        return out

    def attached_macs(self) -> set[str]:
        transports = list(getattr(self.controller, "_transports", None) or ())
        out: set[str] = set()
        for t in transports:
            mac = getattr(t, "ident_mac", None)
            if mac:
                out.add(str(mac).upper())
        return out

    def missing_macs(self) -> set[str]:
        with self._lock:
            return (
                self.expected_macs()
                - self.attached_macs()
                - self._cancelled_macs
            )

    def cancelled_macs(self) -> set[str]:
        with self._lock:
            return set(self._cancelled_macs)

    # ---- core: evaluate + arm/cancel timer -----------------------------

    def evaluate_and_arm(self) -> None:
        """Snapshot the missing set; arm the timer if non-empty, cancel
        otherwise. Broadcasts the current state to the WebUI either
        way so a freshly-emptied missing list clears the banner."""
        with self._lock:
            if self._shutdown:
                self._cancel_timer_locked()
                return
            missing = self.missing_macs()
            if not missing:
                self._cancel_timer_locked()
                self._next_fire_ts = None
                self._broadcast_state_locked()
                return
            if self._timer is None:
                self._arm_locked()
            self._broadcast_state_locked()

    def _arm_locked(self) -> None:
        delay = self._poll_interval_s
        timer = threading.Timer(delay, self._fire)
        timer.daemon = True
        timer.name = "rl-missing-transport-poll"
        self._timer = timer
        self._next_fire_ts = time.time() + delay
        timer.start()

    def _cancel_timer_locked(self) -> None:
        timer = self._timer
        self._timer = None
        if timer is None:
            return
        try:
            timer.cancel()
        except Exception:
            logger.debug(
                "MissingTransportTracker: cancel timer raised",
                exc_info=True,
            )

    def _fire(self) -> None:
        # Drop the timer reference inside the lock so concurrent
        # arm/cancel calls behave correctly. Run soft_rediscover OUTSIDE
        # the lock (it does USB I/O and can take 500 ms+).
        with self._lock:
            self._timer = None
            self._next_fire_ts = None
            if self._shutdown:
                return
            missing = self.missing_macs()
        if not missing:
            self.evaluate_and_arm()
            return
        try:
            soft = getattr(self.controller, "soft_rediscover", None)
            if callable(soft):
                soft()
            else:
                logger.debug(
                    "MissingTransportTracker: controller.soft_rediscover "
                    "missing — skipping poll",
                )
        except Exception:
            logger.exception(
                "MissingTransportTracker: soft_rediscover raised",
            )
        # Re-evaluate to either re-arm (still missing) or clear (all
        # now attached). The broadcast inside evaluate_and_arm carries
        # the post-rediscover state.
        self.evaluate_and_arm()

    # ---- operator-driven controls -------------------------------------

    def cancel(self, ident_mac: Optional[str]) -> None:
        """Suppress polling for ``ident_mac`` (or every currently-missing
        MAC when ``ident_mac is None``). The SetupChangeAssistant exposes
        this via ``POST /api/gateway/cancel-reconnect``."""
        with self._lock:
            if ident_mac is None:
                self._cancelled_macs.update(
                    self.expected_macs() - self.attached_macs()
                )
            else:
                self._cancelled_macs.add(str(ident_mac).upper())
        self.evaluate_and_arm()

    def clear_cancelled(self) -> None:
        """Re-enable polling for previously-cancelled MACs. Called by
        ``POST /api/gateway/rediscover`` so the operator's manual
        trigger overrides earlier cancels."""
        with self._lock:
            self._cancelled_macs.clear()
        self.evaluate_and_arm()

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            self._cancel_timer_locked()

    # ---- SSE payload --------------------------------------------------

    def snapshot(self) -> list[dict]:
        """One row per currently-missing MAC, ordered by MAC for stable
        UI rendering. ``network_id`` / ``network_name`` are resolved via
        the controller's ``network_repository`` so the banner can show
        a friendly name."""
        with self._lock:
            missing = sorted(self.missing_macs())
            next_in: Optional[float] = None
            if self._next_fire_ts is not None:
                next_in = max(0.0, self._next_fire_ts - time.time())
        repo = getattr(self.controller, "network_repository", None)
        get_by_mac = getattr(repo, "get_by_gateway_mac", None) if repo else None
        out: list[dict] = []
        for mac in missing:
            net = None
            if callable(get_by_mac):
                try:
                    net = get_by_mac(mac)
                except Exception:
                    # swallow-ok: repo lookup is best-effort. A missing
                    # network row just means the banner won't carry a
                    # friendly name — the ident_mac is still useful.
                    net = None
            out.append({
                "ident_mac": mac,
                "network_id": str(getattr(net, "id", "") or "") or None if net else None,
                "network_name": str(getattr(net, "name", "") or "") or None if net else None,
                "next_retry_in_s": round(next_in, 3) if next_in is not None else None,
            })
        return out

    def _broadcast_state_locked(self) -> None:
        """Call the broadcaster with the current snapshot. Caller must
        hold ``self._lock``. Failure is swallow-logged — broadcast
        failure must not poison the tracker's polling loop."""
        if not self._broadcast:
            return
        try:
            self._broadcast(self.EVENT_MISSING, {"missing": self.snapshot()})
        except Exception:
            logger.exception(
                "MissingTransportTracker: broadcast(gateway_missing) raised",
            )
