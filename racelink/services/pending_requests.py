"""Host-side request/reply matching registry (plan Transport Redesign B).

The Gateway firmware no longer closes an RX window to signal "the reply you
were waiting for is done" -- it sits in Continuous RX and forwards every
matching frame. The Host therefore owns the "is this frame my awaited reply?"
decision. :class:`PendingRequestRegistry` is the data structure that tracks
outstanding unicast request/response exchanges and unblocks the waiting
caller as soon as a matching reply event arrives.

The matching key is ``(sender_last3, opcode-or-ack-of)``:

* ``RESP_ACK``  -> the reply is ``OPC_ACK`` with ``ack_of == opcode7``.
* ``RESP_SPECIFIC`` -> the reply is the protocol's declared response opcode.

A single registry instance is shared across the GatewayService for all its
outstanding unicast waits. Broadcast collectors (discovery, stream, group
status) do **not** use the registry; they use the separate wall-clock
collector because they expect N replies from unknown senders.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from ..transport.gateway_events import LP

logger = logging.getLogger(__name__)

# Policy codes mirror racelink.protocol.rules (kept as ints so this module
# does not need to import the rules module and we avoid a circular import).
RESP_ACK = 1
RESP_SPECIFIC = 2


@dataclass
class PendingRequest:
    """State for a single outstanding unicast request.

    ``expected_key2`` is an optional secondary discriminator on the
    parsed reply event. When set, ``matches()`` also requires
    ``ev.get(<discriminator_field>) == expected_key2``. The discriminator
    field is selected by the policy:

    * ``RESP_SPECIFIC`` — discriminator field is ``ev["option"]`` (the
      payload byte that GET_CONFIG_REPLY echoes back). This is the
      case the discriminator was added for: two GET_CONFIG requests
      for different options would otherwise wake on FIFO order without
      regard to which option's reply actually arrived.

    Existing callers that pass ``expected_key2=None`` (the default)
    behave exactly as before — no per-candidate filter applied.
    """

    sender_last3: bytes
    expected_key: int  # opcode7 for RESP_ACK (matched against ack_of) or response_opcode for RESP_SPECIFIC
    policy: int
    timeout_s: float
    expected_key2: Optional[int] = None
    done: threading.Event = field(default_factory=threading.Event)
    reply: Optional[dict] = None
    registered_ts: float = field(default_factory=time.monotonic)

    def matches(self, ev: dict) -> bool:
        try:
            sender3 = ev.get("sender3")
            if not isinstance(sender3, (bytes, bytearray)):
                return False
            if bytes(sender3) != self.sender_last3:
                return False
            opc = int(ev.get("opc", -1)) & 0x7F
            if self.policy == RESP_ACK:
                if opc != int(LP.OPC_ACK) or int(ev.get("ack_of", -1)) != self.expected_key:
                    return False
            elif self.policy == RESP_SPECIFIC:
                if opc != self.expected_key:
                    return False
            else:
                return False
            if self.expected_key2 is not None:
                # Currently only RESP_SPECIFIC uses a discriminator
                # (the OPC_GET_CONFIG ``option`` byte). Generalises
                # cleanly if other reply types ever carry a sub-key.
                ev_key2 = ev.get("option")
                if ev_key2 is None:
                    return False
                if int(ev_key2) != int(self.expected_key2):
                    return False
            return True
        except Exception:
            # swallow-ok: malformed event dispatched to us -> "not a match"
            return False


class PendingRequestRegistry:
    """Thread-safe registry of outstanding unicast request/response waits.

    Each ``register`` call returns a :class:`PendingRequest` whose ``done``
    event is set (by :meth:`try_match`) as soon as a matching reply arrives.
    The caller waits on ``done`` and calls :meth:`cancel` in a ``finally``
    block regardless of outcome.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Multiple waits against the same (sender, key) are unlikely in
        # practice, but we keep a list to handle pathological reentrancy.
        self._by_key: dict[tuple[bytes, int, int], list[PendingRequest]] = {}

    def register(
        self,
        *,
        sender_last3: bytes,
        expected_key: int,
        policy: int,
        timeout_s: float,
        expected_key2: Optional[int] = None,
    ) -> PendingRequest:
        req = PendingRequest(
            sender_last3=bytes(sender_last3),
            expected_key=int(expected_key),
            policy=int(policy),
            timeout_s=float(timeout_s),
            expected_key2=None if expected_key2 is None else int(expected_key2),
        )
        key = (req.sender_last3, req.policy, req.expected_key)
        with self._lock:
            self._by_key.setdefault(key, []).append(req)
            pending_total = sum(len(b) for b in self._by_key.values())
        logger.debug(
            "registry.register sender=%s policy=%d expected_key=0x%02X%s timeout=%.3fs pending_total=%d",
            req.sender_last3.hex().upper(),
            req.policy,
            req.expected_key,
            "" if req.expected_key2 is None else f" expected_key2=0x{req.expected_key2:02X}",
            req.timeout_s,
            pending_total,
        )
        return req

    def cancel(self, req: PendingRequest) -> None:
        """Remove ``req`` from the registry; safe to call multiple times."""
        key = (req.sender_last3, req.policy, req.expected_key)
        removed = False
        with self._lock:
            bucket = self._by_key.get(key)
            if bucket:
                try:
                    bucket.remove(req)
                    removed = True
                except ValueError:
                    pass
                if not bucket:
                    self._by_key.pop(key, None)
            pending_total = sum(len(b) for b in self._by_key.values())
        if removed:
            elapsed = time.monotonic() - req.registered_ts
            logger.debug(
                "registry.cancel sender=%s policy=%d expected_key=0x%02X elapsed=%.3fs done=%s pending_total=%d",
                req.sender_last3.hex().upper(),
                req.policy,
                req.expected_key,
                elapsed,
                req.done.is_set(),
                pending_total,
            )

    def try_match(self, ev: dict) -> Optional[PendingRequest]:
        """If ``ev`` satisfies any pending request, complete it and return it.

        Returns the matched request (with ``reply`` and ``done`` set) or
        ``None`` when nothing matched. The matched request is removed from the
        registry so the caller's ``finally: cancel()`` is idempotent.
        """
        try:
            sender3 = ev.get("sender3")
            if not isinstance(sender3, (bytes, bytearray)):
                return None
            sender_bytes = bytes(sender3)
            opc = int(ev.get("opc", -1)) & 0x7F
        except Exception:
            # swallow-ok: malformed event -> nobody can be waiting for it
            return None

        candidates: list[tuple[tuple[bytes, int, int], PendingRequest]] = []
        with self._lock:
            # ACK path: key = (sender, RESP_ACK, ack_of)
            ack_of = ev.get("ack_of")
            if opc == int(LP.OPC_ACK) and ack_of is not None:
                key = (sender_bytes, RESP_ACK, int(ack_of) & 0x7F)
                for req in list(self._by_key.get(key, ())):
                    candidates.append((key, req))
            # Specific-reply path: key = (sender, RESP_SPECIFIC, response_opcode)
            key_spec = (sender_bytes, RESP_SPECIFIC, opc)
            for req in list(self._by_key.get(key_spec, ())):
                candidates.append((key_spec, req))

        for key, req in candidates:
            if not req.matches(ev):
                continue
            req.reply = dict(ev)
            req.done.set()
            elapsed = time.monotonic() - req.registered_ts
            with self._lock:
                bucket = self._by_key.get(key)
                if bucket is not None:
                    try:
                        bucket.remove(req)
                    except ValueError:
                        pass
                    if not bucket:
                        self._by_key.pop(key, None)
            logger.debug(
                "registry.try_match HIT sender=%s policy=%d expected_key=0x%02X elapsed=%.3fs",
                req.sender_last3.hex().upper(),
                req.policy,
                req.expected_key,
                elapsed,
            )
            return req

        # Diagnostic: record when a reply-looking event passed through without
        # matching any waiter. Useful for spotting "ACK arrived but too late"
        # or "ACK for wrong opcode" situations.
        if opc == int(LP.OPC_ACK):
            logger.debug(
                "registry.try_match MISS opc=ACK sender=%s ack_of=%s pending_keys=%d",
                sender_bytes.hex().upper(),
                ev.get("ack_of"),
                len(candidates),
            )
        return None

    def pending_count(self) -> int:
        with self._lock:
            return sum(len(bucket) for bucket in self._by_key.values())
