"""Threaded broadcast fan-out across multiple transports.

When :class:`GatewayService` (and friends) need to send the same M2N
frame to several transports concurrently, this helper does the work:
one thread per transport, all kicked off in quick succession (Phase A —
USB writes dispatch within ~1 ms of each other), then a join cycle
that returns after the slowest gateway's ``EV_TX_DONE`` arrived
(Phase B — collected wall-clock is bounded by the slowest single
LoRa airtime, NOT by N × airtime).

Rationale for threads over a dispatch/await split
-------------------------------------------------
The transport's ``_send_m2n`` holds ``_tx_lock`` across the USB write
AND the wait on ``EV_TX_DONE`` — the same lock the RX reader thread
acquires briefly to populate ``_pending_send_outcome``. Splitting
``_send_m2n`` into separate dispatch + await calls would release the
lock between phases and open a race window in which a second caller
could clobber the outcome slot.

Worker threads sidestep this cleanly: each thread runs the existing
``_send_m2n`` end-to-end, locking discipline unchanged. Across N
transports the only sequential cost is the kernel thread-spawn loop
(~50 µs/thread, in low double-digit µs total for realistic N≤4),
which is dwarfed by even a single USB-CDC round-trip (~sub-ms) and
by LoRa airtime (~10..150 ms depending on SF / payload).

Cross-network simultaneity
--------------------------
For ``arm_on_sync``-driven scene coordination across networks, the
wall-clock skew between Gateway-1's air time and Gateway-N's air time
is what determines how synchronously the armed effects fire. With this
helper the skew is bounded by Thread.start() latency (~10 µs each) —
versus N × ~50 ms with the previous sequential-blocking fan-out.

Threading model
---------------
* Each worker thread is daemon=True so a stuck send never holds the
  process open past shutdown.
* Worker exceptions are captured + logged; one transport's failure
  does NOT abort the others' joins. The caller sees the per-transport
  outcome list (``None`` for transports whose worker raised).
* The helper uses ``join(timeout=...)`` to enforce an upper bound on
  the total fan-out time. Defaults to a couple seconds past the
  longest expected airtime; can be overridden per call.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence

from .broadcast_target import BroadcastTarget

logger = logging.getLogger(__name__)


# Default upper bound on fan-out wall-clock. Picked to comfortably
# accommodate the longest expected single-radio send (SF12 max-len
# packets push toward 1.5 s; SF7 small-frame is <100 ms) plus host-side
# slack. Callers can override via the ``timeout_s`` kwarg.
DEFAULT_FANOUT_TIMEOUT_S: float = 3.0


@dataclass
class FanoutResult:
    """Per-transport outcome of a :func:`broadcast_fanout` call.

    ``outcome`` is whatever the worker callable returned (typically a
    ``SendOutcome``). ``error`` carries the captured exception when the
    worker raised; ``timed_out`` is True when ``Thread.join`` returned
    without the worker completing within ``timeout_s``.
    """

    transport: Any
    outcome: Any = None
    error: Optional[BaseException] = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        if self.error is not None or self.timed_out:
            return False
        # Treat None outcome as "no opinion" — only False-y SendOutcome
        # objects (REJECTED/TIMEOUT/USB_ERROR) should be marked not-ok.
        if self.outcome is None:
            return True
        ok_attr = getattr(self.outcome, "ok", None)
        if ok_attr is None:
            return True
        return bool(ok_attr)


def broadcast_fanout(
    transports: Sequence[Any],
    work_fn: Callable[[Any], Any],
    *,
    timeout_s: float = DEFAULT_FANOUT_TIMEOUT_S,
    label: str = "broadcast_fanout",
) -> List[FanoutResult]:
    """Run ``work_fn(transport)`` on each transport in parallel; return
    per-transport :class:`FanoutResult` once every worker has joined
    (or timed out).

    ``work_fn`` should be a small closure that knows what to send on a
    transport — e.g. ``lambda t: t.send_sync(recv3=..., ts24=..., ...)``.
    The helper does NOT inspect or transform the return value; the
    caller can ignore it (fire-and-forget) or use the per-transport
    outcome objects in the result list.

    Threading guarantees:
        * Workers run on daemon threads, so a hung send never blocks
          process shutdown.
        * Each worker holds its own transport's ``_tx_lock`` (via the
          existing ``_send_m2n`` discipline). Two workers on the SAME
          transport would serialise on that lock — by construction we
          never schedule two workers on one transport here, since the
          transport list comes from a deduplicated network-id set.
        * Worker exceptions are captured per-thread; the helper logs
          them and continues to join the rest. Caller can inspect each
          ``FanoutResult.error``.

    Return order matches ``transports`` input order (NOT join order).
    Useful for downstream callers that want to correlate outcomes to
    networks by index.
    """
    if not transports:
        return []

    results: List[FanoutResult] = [FanoutResult(transport=t) for t in transports]
    threads: List[threading.Thread] = []

    def _runner(idx: int, t: Any) -> None:
        try:
            results[idx].outcome = work_fn(t)
        except BaseException as exc:  # noqa: BLE001 — capture everything
            results[idx].error = exc
            logger.exception("%s: worker raised on transport %r",
                              label, getattr(t, "ident_mac", None))

    # Phase A: dispatch — start every thread in quick succession. Each
    # Thread.start() returns once the OS thread is spawned (~10 µs);
    # the worker's USB write happens on the new thread shortly after.
    for idx, t in enumerate(transports):
        th = threading.Thread(
            target=_runner,
            args=(idx, t),
            name=f"{label}-w{idx}",
            daemon=True,
        )
        th.start()
        threads.append(th)

    # Phase B: collect — join every thread. Per-thread timeout is the
    # full budget because the slowest thread bounds the wall-clock. A
    # truly-stalled worker is marked ``timed_out`` and the helper
    # returns — the daemon thread continues in the background until
    # its underlying send completes or the process exits.
    for idx, th in enumerate(threads):
        th.join(timeout=timeout_s)
        if th.is_alive():
            results[idx].timed_out = True
            logger.warning(
                "%s: transport %r did not finish within %.2fs; "
                "leaving worker to drain in background.",
                label,
                getattr(transports[idx], "ident_mac", None),
                timeout_s,
            )

    return results


def resolve_broadcast_transports(
    controller: Any,
    target: Optional[BroadcastTarget],
    *,
    label: str,
    fallback_transport: Any = None,
) -> list:
    """Resolve a :class:`BroadcastTarget` to a list of live transports
    via ``controller.transport_for_network``.

    Shared helper used by :class:`GatewayService` and
    :class:`ControlService` so the resolve-and-warn behaviour is
    uniform across broadcast send paths.

    * ``target=None`` (deprecated): returns every attached transport
      and emits a deprecation warning when there are 2+ transports.
      Existing call sites relied on this implicit "all" behaviour;
      callers should be migrated to pass ``target`` explicitly so
      cross-network broadcasts are intentional.
    * ``target=BroadcastTarget(...)``: each network id resolves via
      ``controller.transport_for_network``; missing networks are
      skipped with a warning. Duplicate transports (two ids mapping
      to the same physical transport) are coalesced.
    * Empty result is returned (and logged) without raising — callers
      treat "no addressable transport" as a no-op send.

    ``fallback_transport`` is used only when ``target=None`` AND
    ``controller.transports`` is empty — typically
    ``self.transport`` from the service, which mirrors the
    single-gateway legacy behaviour during reconnect windows.
    """
    if controller is None:
        logger.warning("%s: controller not available", label)
        return []

    if target is None:
        attached = list(getattr(controller, "transports", None) or ())
        if not attached:
            if fallback_transport is None:
                logger.warning("%s: communicator not ready", label)
                return []
            attached = [fallback_transport]
        if len(attached) > 1:
            logger.warning(
                "%s broadcast called without explicit target — defaulting "
                "to all %d attached transports. Caller should pass target= "
                "explicitly so cross-network broadcasts are intentional.",
                label, len(attached),
            )
        return attached

    out: list = []
    seen: set = set()
    for nid in target.network_ids:
        t = controller.transport_for_network(nid)
        if t is None:
            logger.warning(
                "%s: network %s has no attached transport — skipping",
                label, nid,
            )
            continue
        ident = id(t)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(t)
    if not out:
        logger.warning(
            "%s: no addressable transport for target=%r — no-op",
            label, target.network_ids,
        )
    return out
