"""Host-side request/reply matching registry — unified matcher (Option D).

The Gateway firmware sits in Continuous RX and forwards every matching frame to
the Host, so the Host owns the "is this frame an awaited reply?" decision.
:class:`PendingMatcher` describes a single outstanding receive expectation
(unicast 1-reply, multi-sender N-reply collector, or wildcard discovery), and
:class:`PendingMatcherRegistry` is the data structure that routes inbound
frames to the matchers that want them.

Replaces two earlier mechanisms that lived side-by-side
(``PendingRequestRegistry`` for single-sender unicast and an
``add_listener``-backed collector for N-reply broadcasts). See the plan at
``.claude/plans/aktuell-teste-ich-den-lazy-willow.md`` for the design rationale
and migration sequence.

A single registry instance is shared across the GatewayService for all
outstanding waits; the ``GatewayService.on_transport_event`` hook funnels each
inbound event through :meth:`PendingMatcherRegistry.try_match`. The waiter's
:meth:`PendingMatcher.wait` (or the high-level :meth:`GatewayService.send_and_match`)
blocks on the matcher's condition until the matcher is done, the idle window
closes, or the hard ceiling elapses.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..transport.gateway_events import LP

logger = logging.getLogger(__name__)

# Policy codes mirror racelink.protocol.rules (kept as ints so this module
# does not need to import the rules module and we avoid a circular import).
# Retained for callers that still think in policy terms — the matcher itself
# distinguishes via ``expected_ack_of`` vs ``expected_opcode``.
RESP_ACK = 1
RESP_SPECIFIC = 2


@dataclass
class PendingMatcher:
    """One outstanding receive expectation.

    Filters (all set fields must hold for an event to match):

    * ``sender_filter`` — accepted sender-last3 set. ``None`` = wildcard (any
      sender). A singleton frozenset enables fast-bucket lookup in the
      registry; larger sets fall back to the broadcast list.
    * ``expected_opcode`` — for ``RESP_SPECIFIC``-style replies (e.g.
      ``OPC_DEVICES`` reply, ``GET_CONFIG_REPLY``). ``None`` = wildcard.
    * ``expected_ack_of`` — for ``RESP_ACK``-style replies: the original
      request's opcode7 that an ``OPC_ACK`` will echo back via ``ack_of``.
      When set the matcher only accepts ``OPC_ACK`` frames whose
      ``ack_of`` equals this value.
    * ``discriminator_field`` / ``discriminator_value`` — optional final
      filter on a parsed event field (today only ``"option"`` for
      GET_CONFIG_REPLY to prevent concurrent reads on the same device but
      for different options from waking each other).

    Collection semantics:

    * ``expected_count`` — how many matching events to collect before the
      matcher is "done". 1 = traditional unicast request/reply. >1 = group
      collector.
    * ``idle_timeout_s`` — after the first match, the matcher is considered
      idle-complete if no further match arrives within this many seconds.
      0.0 disables idle-mode (only ``max_timeout_s`` applies — original
      unicast semantics).
    * ``max_timeout_s`` — hard ceiling from the moment the matcher is
      registered. Always applies.

    The registry mutates ``collected`` / ``last_match_ts`` / ``done`` under
    the matcher's internal condition; callers should only read these fields
    inside ``with matcher._cond:`` or after ``wait`` has returned.
    """

    # --- filters ---
    sender_filter: Optional[frozenset[bytes]] = None
    expected_opcode: Optional[int] = None
    expected_ack_of: Optional[int] = None
    discriminator_field: Optional[str] = None
    discriminator_value: Optional[Any] = None
    # Optional Stage-2 multi-network filter. ``None`` means "wildcard"
    # — keeps Stage-2 fully backwards-compatible with the single-
    # gateway code paths that don't yet know about gateway_id. Stage 3
    # makes this required for every unicast expectation.
    gateway_id: Optional[str] = None

    # --- collection semantics ---
    expected_count: int = 1
    idle_timeout_s: float = 0.0
    max_timeout_s: float = 1.0

    # --- state (registry-managed) ---
    collected: list[dict] = field(default_factory=list)
    last_match_ts: Optional[float] = None
    registered_ts: float = field(default_factory=time.monotonic)

    # --- sync primitives (private) ---
    _cond: threading.Condition = field(default_factory=threading.Condition, repr=False)
    _done: bool = field(default=False, repr=False)

    def matches(self, ev: dict) -> bool:
        """Full filter evaluation. Registry pre-filters via bucket lookup."""
        try:
            # Gateway-id filter (Stage 2). When set, only events from the
            # matching transport count. The transport tags every event it
            # emits with ``gateway_id`` (its ident_mac); an event without
            # that tag is treated as "comes from the legacy single
            # transport" and matches a None filter, but never a concrete
            # gateway_id filter.
            if self.gateway_id is not None:
                ev_gw = ev.get("gateway_id")
                if ev_gw is None or str(ev_gw) != str(self.gateway_id):
                    return False
            # Sender filter
            if self.sender_filter is not None:
                s3 = ev.get("sender3")
                if not isinstance(s3, (bytes, bytearray)):
                    return False
                if bytes(s3) not in self.sender_filter:
                    return False
            opc = int(ev.get("opc", -1)) & 0x7F
            # ACK-of filter (implies opc == OPC_ACK)
            if self.expected_ack_of is not None:
                if opc != int(LP.OPC_ACK):
                    return False
                if int(ev.get("ack_of", -1)) != int(self.expected_ack_of):
                    return False
            elif self.expected_opcode is not None:
                # Specific-reply opcode filter
                if opc != int(self.expected_opcode):
                    return False
            # Discriminator (e.g. "option" for GET_CONFIG_REPLY, "reply" for IDENTIFY).
            # Numeric values are int-coerced for robustness against codec
            # variants that surface ints as strings; non-numeric values
            # (e.g. the "reply" marker string) compare for equality as-is.
            if self.discriminator_field is not None:
                ev_val = ev.get(self.discriminator_field)
                if ev_val is None:
                    return False
                if isinstance(self.discriminator_value, int):
                    try:
                        if int(ev_val) != int(self.discriminator_value):
                            return False
                    except (TypeError, ValueError):
                        return False
                else:
                    if ev_val != self.discriminator_value:
                        return False
            return True
        except Exception:
            # swallow-ok: a malformed event is "not a match"
            return False

    @property
    def done(self) -> bool:
        """True if the matcher has collected ``expected_count`` events or
        was otherwise marked done."""
        return self._done or len(self.collected) >= self.expected_count

    def wait(self, *, on_send_complete: Optional[float] = None) -> str:
        """Block until done, idle window closes, or max_timeout elapses.

        Returns the termination reason: ``"count"`` (expected reached),
        ``"idle"`` (silence after first match), ``"max_timeout"`` (ceiling
        with at least one match), ``"no_reply"`` (ceiling without any match).

        ``on_send_complete`` is the monotonic-time anchor for the hard
        ceiling. If ``None``, the matcher's ``registered_ts`` is used. Callers
        that want the hard ceiling to start counting only after their
        ``send_fn`` returned should pass ``time.monotonic()`` taken right
        after the send.
        """
        t_anchor = on_send_complete if on_send_complete is not None else self.registered_ts
        hard_deadline = t_anchor + float(self.max_timeout_s)

        with self._cond:
            while True:
                if self.done:
                    return "count"
                now = time.monotonic()
                if now >= hard_deadline:
                    return "max_timeout" if self.last_match_ts is not None else "no_reply"
                if self.last_match_ts is not None and self.idle_timeout_s > 0.0:
                    idle_deadline = self.last_match_ts + float(self.idle_timeout_s)
                    effective = min(idle_deadline, hard_deadline)
                    wait_s = effective - now
                    if wait_s <= 0.0:
                        return "idle"
                else:
                    wait_s = hard_deadline - now
                self._cond.wait(timeout=max(0.0, wait_s))


class PendingMatcherRegistry:
    """Thread-safe registry of outstanding matchers.

    Lookup strategy:

    * **Unicast fast-path**: matchers with ``len(sender_filter) == 1`` and
      a concrete ``expected_ack_of`` / ``expected_opcode`` are placed in a
      dict bucket keyed by ``(sender_last3, key)``. O(1) lookup on inbound
      events whose ``sender3`` + ``opc``/``ack_of`` hit the same key.
    * **Broadcast slow-path**: all other matchers (wildcard sender, multi-
      sender set, wildcard opcode) live in a list; ``try_match`` does a
      linear scan after the unicast bucket misses. The list is expected to
      stay small (≤ a handful of outstanding ops at any one time).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._unicast: dict[tuple[bytes, int], list[PendingMatcher]] = {}
        self._broadcast: list[PendingMatcher] = []

    @staticmethod
    def _unicast_key(m: PendingMatcher) -> Optional[tuple[bytes, int]]:
        """Returns the fast-bucket lookup key if the matcher qualifies."""
        if m.sender_filter is None or len(m.sender_filter) != 1:
            return None
        # Prefer ack_of (ACK path) because it discriminates within a single
        # sender far better than opc (which is always OPC_ACK for that path).
        key = m.expected_ack_of if m.expected_ack_of is not None else m.expected_opcode
        if key is None:
            return None
        sender = next(iter(m.sender_filter))
        return (sender, int(key) & 0x7F)

    def register(self, m: PendingMatcher) -> PendingMatcher:
        """Insert ``m`` into the appropriate bucket. Returns ``m`` for chaining."""
        with self._lock:
            uk = self._unicast_key(m)
            if uk is not None:
                self._unicast.setdefault(uk, []).append(m)
            else:
                self._broadcast.append(m)
            pending_total = sum(len(b) for b in self._unicast.values()) + len(self._broadcast)
        logger.debug(
            "matcher.register sender_filter=%s opcode=%s ack_of=%s expected=%d "
            "idle=%.2fs max=%.2fs pending_total=%d",
            "any" if m.sender_filter is None else [s.hex().upper() for s in m.sender_filter],
            m.expected_opcode,
            m.expected_ack_of,
            m.expected_count,
            m.idle_timeout_s,
            m.max_timeout_s,
            pending_total,
        )
        return m

    def cancel(self, m: PendingMatcher) -> None:
        """Remove ``m`` from its bucket; idempotent."""
        removed = False
        with self._lock:
            uk = self._unicast_key(m)
            if uk is not None:
                bucket = self._unicast.get(uk)
                if bucket:
                    try:
                        bucket.remove(m)
                        removed = True
                    except ValueError:
                        pass
                    if not bucket:
                        self._unicast.pop(uk, None)
            else:
                try:
                    self._broadcast.remove(m)
                    removed = True
                except ValueError:
                    pass
            pending_total = sum(len(b) for b in self._unicast.values()) + len(self._broadcast)
        if removed:
            elapsed = time.monotonic() - m.registered_ts
            logger.debug(
                "matcher.cancel collected=%d done=%s elapsed=%.3fs pending_total=%d",
                len(m.collected),
                m.done,
                elapsed,
                pending_total,
            )

    def try_match(self, ev: dict) -> Optional[PendingMatcher]:
        """Find the first matcher that accepts ``ev``. Append to its
        collected list, update timestamps, signal its condition. Returns
        the matched matcher (with state updated) or ``None``.

        Lookup order: unicast bucket (fast) → broadcast list (linear). The
        first matching matcher consumes the event; we do not fan out to
        multiple matchers on the same event (this preserves today's "ACK
        matches exactly one waiter" semantics).
        """
        try:
            sender3 = ev.get("sender3")
            opc = int(ev.get("opc", -1)) & 0x7F
            ack_of = ev.get("ack_of")
        except Exception:
            # swallow-ok: malformed event - nobody can match it
            return None

        candidates: list[PendingMatcher] = []
        with self._lock:
            if isinstance(sender3, (bytes, bytearray)):
                s3b = bytes(sender3)
                # ACK path: ack_of is the discriminator in the bucket key.
                if opc == int(LP.OPC_ACK) and ack_of is not None:
                    candidates.extend(self._unicast.get((s3b, int(ack_of) & 0x7F), ()))
                # Specific-reply path: opc is the discriminator.
                candidates.extend(self._unicast.get((s3b, opc), ()))
            # Broadcast list — small, linear scan.
            candidates.extend(self._broadcast)

        matched_any_candidate = len(candidates) > 0
        for m in candidates:
            if not m.matches(ev):
                continue
            with m._cond:
                m.collected.append(dict(ev))
                m.last_match_ts = time.monotonic()
                if len(m.collected) >= m.expected_count:
                    m._done = True
                m._cond.notify_all()
            logger.debug(
                "matcher.try_match HIT collected=%d/%d expected_ack_of=%s expected_opcode=%s",
                len(m.collected),
                m.expected_count,
                m.expected_ack_of,
                m.expected_opcode,
            )
            return m

        # Diagnostic: only log when a matcher in the unicast bucket had the
        # right key but its full filter rejected the event — that is the
        # genuinely interesting "ACK arrived but with wrong discriminator"
        # case. When no candidates existed at all the event simply has no
        # waiter (e.g. unsolicited STATUS_REPLY, untracked broadcast); no
        # log noise.
        if opc == int(LP.OPC_ACK) and matched_any_candidate:
            logger.debug(
                "matcher.try_match NO_MATCH opc=ACK sender=%s ack_of=%s "
                "candidates=%d (had right bucket key, full filter failed)",
                bytes(sender3).hex().upper() if isinstance(sender3, (bytes, bytearray)) else "?",
                ack_of,
                len(candidates),
            )
        return None

    def pending_count(self) -> int:
        with self._lock:
            return sum(len(b) for b in self._unicast.values()) + len(self._broadcast)
