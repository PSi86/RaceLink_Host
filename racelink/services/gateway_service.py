"""Gateway orchestration service: transport events, reply matching, lifecycle.

The single largest service in the host. Owns:

* The **pending-matcher registry** (:class:`PendingMatcherRegistry` from
  ``pending_requests``) used for unicast and multi-sender request/reply
  matching via ``send_and_match``.
* The **TX listener path** (``on_transport_tx``) that stamps a pending
  expectation when a unicast request goes out, and the matching
  **RX listener path** (``on_transport_event`` →
  ``pending_try_match`` / ``pending_window_closed``) that clears the
  expectation on a reply or window-closed.
* **Reconnect** (``schedule_reconnect``) and **auto-restore**
  (``_spawn_auto_reassign_worker`` via a bounded
  ``ThreadPoolExecutor``) when the gateway disconnects or a node
  comes back with the wrong groupId.
* **High-level dispatch helpers**: ``send_config``, ``send_sync``,
  ``send_stream`` — orchestrate the transport's primitive
  ``send_*`` ops with retries and ACK-collection.

Threading: this module is the host's primary cross-thread surface.
The transport's RX reader thread fans out to ``on_transport_event``;
web request threads + the scene runner call the dispatch helpers
synchronously. Audit findings A1–A6 + B5 (see the active project-
wide audit plan in ``.claude/plans/``) added the TX-serialization
lock, the ``_pending_config_lock`` and ``_pending_expect_lock``,
and the SSE broadcast lock-discipline fix that this service
depends on.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

from ..domain import create_device, default_device_name, get_dev_type_info
from ..protocol import opcode_name as protocol_opcode_name
from ..protocol import request_direction, response_opcode, response_policy, rules as protocol_rules
from ..transport.framing import mac_last3_from_hex
from ..transport.gateway_events import (
    EV_ERROR,
    EV_RF_CHANGED,
    EV_STATE_CHANGED,
    EV_STATE_REPORT,
    EV_TX_DONE,
    EV_TX_REJECTED,
    GATEWAY_STATE_IDLE,
    GATEWAY_STATE_NAME,
    GATEWAY_STATE_RX,
    GATEWAY_STATE_RX_WINDOW,
    GATEWAY_STATE_UNKNOWN,
    LP,
    RF_CHANGE_OK,
    RF_CHANGE_REASON_NAME,
)
from .pending_requests import (
    PendingMatcher,
    PendingMatcherRegistry,
)
from . import rf_timing

logger = logging.getLogger(__name__)


class _NullLock:
    """Fallback context manager used when no state lock is available."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class GatewayService:
    def __init__(self, controller):
        self.controller = controller
        self._auto_reassign_cooldown_s = 2.0
        # Stage 2 Part 3: cooldown keyed by ``(gateway_id, mac)``. The
        # leading ``Optional[str]`` element is ``None`` for the legacy
        # single-gateway code path (gateway_id unknown / wildcard) and
        # the transport's ``ident_mac`` once events carry the tag.
        self._auto_reassign_recent: dict[tuple[Optional[str], str], float] = {}
        self._auto_reassign_lock = threading.Lock()
        # A7: bounded executor for auto-restore workers. The previous
        # implementation kept a ``list[Thread]`` and pruned dead
        # entries on every spawn — which never pruned if no further
        # spawns happened, leaving up to N+1 dead Thread objects in
        # memory until process exit. ``ThreadPoolExecutor`` caps
        # concurrent work at ``max_workers`` (8 is plenty for typical
        # fleets) and reuses threads across submissions; an idle pool
        # holds 0 active threads.
        #
        # ``_auto_restore_futures`` keeps the in-flight futures so the
        # test hook ``_join_auto_restore_workers`` can wait on them
        # deterministically (mirrors the previous behaviour).
        self._auto_restore_executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="rl-auto-restore",
        )
        self._auto_restore_futures: list[Future] = []
        # Per-MAC tracking so OTA can wait specifically on the auto-restore
        # worker for the device it just flashed, rather than the global
        # list (which may include unrelated workers). Keyed by 12-char
        # uppercase MAC hex. See ``wait_for_auto_restore``.
        self._auto_restore_futures_by_mac: dict[str, Future] = {}
        # Per-MAC threading.Event signalling "device sent IDENTIFY_REPLY".
        # Used by OTA to gate "next device starts" on "previous device
        # rebooted and re-registered on RaceLink radio". The event is
        # cleared by the OTA workflow before each AP-Open and set in
        # ``on_transport_event`` when an IDENTIFY_REPLY arrives.
        self._identify_events: dict[str, threading.Event] = {}
        self._identify_events_lock = threading.Lock()
        # Transport redesign (plan Phase B): the Host owns request/reply
        # matching now that the Gateway stays in Continuous RX. The registry
        # unblocks unicast waiters as soon as the expected frame arrives; any
        # unmatched frame continues through the existing unsolicited pipeline
        # in ``on_transport_event``.
        #
        # Stage 2 Part 3: the registry is now keyed per-transport (by
        # ``gateway_id`` = transport.ident_mac). At N=1 attached gateway,
        # every matcher/event lands in the same default-bucket and the
        # behaviour is byte-identical to Stage 2 Part 2. With multiple
        # transports, matchers tagged with a concrete ``gateway_id``
        # only see events from their own transport, eliminating the
        # bucket-key collision risk for two devices that share the
        # same last-3 MAC bytes across different gateways.
        self._pending_registries: dict[Optional[str], PendingMatcherRegistry] = {}
        self._pending_registries_lock = threading.Lock()
        # Default / wildcard registry: matchers with ``gateway_id=None``
        # (legacy single-gateway code paths) live here. Eagerly created
        # so ``self._pending_registry`` always resolves without locking.
        self._pending_registries[None] = PendingMatcherRegistry()
        # Reserved for future use; disabled by default because the observed
        # "bulk set group times out on the second device" problem turned out
        # to be a host-side deadlock (web thread held ``ctx.rl_lock`` across
        # the blocking wait, starving the reader thread in
        # ``handle_ack_event``). See ``_apply_device_meta_updates`` in
        # ``racelink/web/api.py`` for the lock-scope fix. Keep this knob at
        # 0.0 unless a separate diagnostic specifically warrants it.
        self.post_match_settle_s: float = 0.0

    @property
    def transport(self):
        return getattr(self.controller, "transport", None)

    @property
    def _pending_registry(self) -> PendingMatcherRegistry:
        """Back-compat accessor for the default / wildcard registry.

        Stage 2 Part 3 keeps a per-gateway dict of registries; this
        property returns the ``gateway_id=None`` bucket, which is the
        bucket the single-gateway code paths used before the split.
        Tests and any non-routing call site can keep using this name.
        """
        return self._pending_registries[None]

    def _registry_for(self, gateway_id: Optional[str]) -> PendingMatcherRegistry:
        """Return (lazily creating) the registry for ``gateway_id``.

        ``None`` always resolves to the default/wildcard registry that
        was created eagerly in ``__init__``. A concrete ``ident_mac``
        gets its own registry so the fast-bucket lookup keys cannot
        collide across transports that happen to share the same
        last-3 MAC bytes on different devices.
        """
        if gateway_id is None:
            return self._pending_registries[None]
        with self._pending_registries_lock:
            reg = self._pending_registries.get(gateway_id)
            if reg is None:
                reg = PendingMatcherRegistry()
                self._pending_registries[gateway_id] = reg
            return reg

    def _state_lock(self):
        """Return the state-repository mutation lock, or a no-op fallback.

        Callers use this as a context manager to serialize device/group
        mutations that race with web-thread reads (plan P1-4).
        """
        repo = getattr(self.controller, "state_repository", None)
        lock = getattr(repo, "lock", None) if repo is not None else None
        if lock is None:
            return _NullLock()
        return lock

    def send_and_match(
        self,
        send_fn,
        matcher: PendingMatcher,
        *,
        transport=None,
    ) -> tuple[list[dict], str]:
        """Unified send + wait-for-matching-events primitive.

        Registers ``matcher`` with the host's pending-matcher registry,
        invokes ``send_fn``, and blocks on the matcher's condition until
        one of: ``expected_count`` events collected, idle window expires,
        or hard ceiling reached.

        Returns ``(collected_events, reason)`` where ``reason ∈
        {"count", "idle", "max_timeout", "no_reply"}``. The matcher is
        always removed from the registry in the ``finally`` block — safe
        to call multiple times if needed.

        The single primitive for all "outbound TX → inbound reply"
        coordination — covers unicast 1-reply, multi-sender N-reply,
        and wildcard discovery flows.

        ``transport``: Stage 2 Part 3 hook for explicit multi-network
        routing. When supplied, hooks are installed on that specific
        transport and the matcher inherits its ``ident_mac`` as the
        ``gateway_id`` filter (so cross-transport replies cannot
        accidentally satisfy the wait). Defaults to ``self.transport``
        which is ``_transports[0]`` — i.e. legacy single-gateway
        behaviour.
        """
        if transport is None:
            transport = self.transport
        if not transport:
            return [], "no_reply"

        # If the caller did not pre-set ``matcher.gateway_id`` and the
        # routed transport has a known identity, tag the matcher so
        # the per-gateway registry routes events from sibling
        # transports past it. Concrete-id callers (e.g. tests) keep
        # whatever value they put on the matcher.
        if matcher.gateway_id is None:
            ident = getattr(transport, "ident_mac", None)
            if ident:
                matcher.gateway_id = ident

        self.install_transport_hooks(transport=transport)
        registry = self._registry_for(matcher.gateway_id)
        registry.register(matcher)
        t_start = time.monotonic()
        try:
            send_fn()
            # Hard ceiling counts from when send_fn returned — we anchor
            # on registered_ts which is set in ``PendingMatcher.__init__``
            # just before ``register``. Slight
            # drift (≤ ms) is irrelevant.
            reason = matcher.wait(on_send_complete=t_start)
        finally:
            registry.cancel(matcher)
        logger.debug(
            "send_and_match EXIT reason=%s collected=%d elapsed=%.3fs",
            reason,
            len(matcher.collected),
            time.monotonic() - t_start,
        )
        return list(matcher.collected), reason

    def send_broadcast_and_match(
        self,
        send_factory,
        matcher: PendingMatcher,
    ) -> tuple[list[dict], str]:
        """Broadcast variant of :meth:`send_and_match` — fan out across
        every attached transport, collect replies through a single
        wildcard matcher.

        ``send_factory(transport)`` is invoked once per transport in
        ``controller.transports`` to fire the broadcast through that
        specific port. The matcher must use ``gateway_id=None``
        (wildcard) so it collects replies from any transport; this
        helper enforces that constraint defensively. RX events from
        every transport land in the default registry where the
        wildcard matcher lives — see ``on_transport_event``'s dispatch
        that always offers each event to the ``None`` bucket.

        At N=1 attached gateway this collapses to one ``send_factory``
        call and is byte-identical to ``send_and_match``. Stage 3
        callers (discovery, multi-network broadcast streams) swap
        their fixed ``send_fn`` over to this fan-out.
        """
        transports = list(getattr(self.controller, "transports", None) or [])
        if not transports:
            return [], "no_reply"
        # The matcher must be wildcard so replies from any transport
        # can collect into it; force-clear any concrete gateway_id.
        matcher.gateway_id = None
        for t in transports:
            self.install_transport_hooks(transport=t)
        registry = self._registry_for(None)
        registry.register(matcher)
        t_start = time.monotonic()
        try:
            for t in transports:
                try:
                    send_factory(t)
                except Exception:
                    logger.exception(
                        "send_broadcast_and_match: send_factory raised for transport %r",
                        getattr(t, "ident_mac", "?"),
                    )
            reason = matcher.wait(on_send_complete=t_start)
        finally:
            registry.cancel(matcher)
        logger.debug(
            "send_broadcast_and_match EXIT reason=%s collected=%d transports=%d elapsed=%.3fs",
            reason,
            len(matcher.collected),
            len(transports),
            time.monotonic() - t_start,
        )
        return list(matcher.collected), reason

    def _build_unicast_matcher(
        self,
        recv3: bytes,
        opcode7: int,
        timeout_s: float,
        discriminator: Optional[int] = None,
        *,
        gateway_id: Optional[str] = None,
    ) -> Optional[PendingMatcher]:
        """Build a single-reply matcher for unicast request/response.

        Returns ``None`` when the opcode has no expected reply
        (``RESP_NONE`` policy) — the caller should fire-and-forget in
        that case. Broadcast targets (``recv3 == FFFFFF``) get a
        wildcard ``sender_filter`` with idle-timeout enabled so the
        first reply wins and stragglers tail off cleanly.

        ``gateway_id`` (Stage 3): the ``ident_mac`` of the routed
        transport, stamped onto the matcher so the registry refuses
        replies from sibling transports. Required for unicast
        targets (non-broadcast ``recv3``); pure-broadcast matchers
        (``recv3 == FFFFFF``) keep ``gateway_id=None`` as the
        wildcard semantics.
        """
        opcode7 = int(opcode7) & 0x7F
        recv3_b = bytes(recv3 or b"")
        sender_filter_bytes = recv3_b if recv3_b and recv3_b != b"\xFF\xFF\xFF" else None

        try:
            rule = protocol_rules.find_rule(opcode7)
        except Exception:
            # swallow-ok: unknown opcode -> no rule -> caller downgrades policy to RESP_NONE
            rule = None

        policy = int(response_policy(opcode7)) if rule else int(protocol_rules.RESP_NONE)
        if policy == int(protocol_rules.RESP_NONE):
            return None

        sender_filter = (
            frozenset({sender_filter_bytes}) if sender_filter_bytes is not None else None
        )
        idle_s = rf_timing.COLLECT_IDLE_TIMEOUT_S if sender_filter is None else 0.0

        # Stage 3: concrete sender_filter requires gateway_id; the
        # registry's ``register`` would otherwise raise. We let the
        # error propagate from there rather than swallowing here so
        # the call-site stacktrace is meaningful when this misfires.
        matcher_gateway_id = gateway_id if sender_filter is not None else None

        if policy == int(protocol_rules.RESP_ACK):
            return PendingMatcher(
                sender_filter=sender_filter,
                expected_ack_of=opcode7,
                gateway_id=matcher_gateway_id,
                expected_count=1,
                idle_timeout_s=idle_s,
                max_timeout_s=float(timeout_s),
            )
        # RESP_SPECIFIC
        rsp_opc = int(response_opcode(opcode7)) & 0x7F if rule else opcode7
        return PendingMatcher(
            sender_filter=sender_filter,
            expected_opcode=rsp_opc,
            gateway_id=matcher_gateway_id,
            discriminator_field="option" if discriminator is not None else None,
            discriminator_value=discriminator,
            expected_count=1,
            idle_timeout_s=idle_s,
            max_timeout_s=float(timeout_s),
        )

    def send_and_wait_with_retries(
        self,
        recv3: bytes,
        opcode7: int,
        send_fn,
        *,
        attempts: Optional[int] = None,
        per_attempt_timeout_s: Optional[float] = None,
        retry_delay_s: Optional[float] = None,
        transport=None,
    ) -> tuple[list[dict], bool]:
        """Wait-for-reply with bounded retries on transient timeout.

        Builds a unicast :class:`PendingMatcher` per attempt and
        delegates to :meth:`send_and_match`. The per-attempt timeout is
        short (``rf_timing.UNICAST_ATTEMPT_TIMEOUT_S``, default 1.5 s); a
        single dropped frame on either direction triggers an automatic
        retry rather than a false-negative timeout for the caller.
        Success on any attempt short-circuits.

        Defaults pulled from :mod:`rf_timing`. Worst case
        ≈ ``per_attempt × attempts + retry_delay × (attempts - 1)``,
        which with the defaults is ~4.7 s — *shorter* than the
        old 8 s single-attempt timeout this helper replaces, even
        for genuinely-offline devices.

        ``transport``: Stage 2 Part 3 multi-network routing hook. When
        ``None`` (the default), the unicast target's transport is
        derived from ``controller.transport_for_device(recv3)``. At
        N=1 attached gateway this resolves to the same transport every
        caller already closed over in ``send_fn`` — behaviour is
        identical to the pre-Part-3 path. Callers that already chose a
        transport explicitly (e.g. ``setNodeGroupId``) pass it through
        so the matcher's ``gateway_id`` tag matches the actual sender.
        """
        opcode7 = int(opcode7) & 0x7F
        n = int(attempts if attempts is not None else rf_timing.UNICAST_MAX_ATTEMPTS)
        if n < 1:
            n = 1
        per = float(
            per_attempt_timeout_s
            if per_attempt_timeout_s is not None
            else rf_timing.UNICAST_ATTEMPT_TIMEOUT_S
        )
        delay = float(
            retry_delay_s if retry_delay_s is not None else rf_timing.UNICAST_RETRY_DELAY_S
        )

        # Resolve the optional sender device once for the mark_online hook.
        recv3_b = bytes(recv3 or b"")
        sender_filter_bytes = recv3_b if recv3_b and recv3_b != b"\xFF\xFF\xFF" else None
        sender_filter_hex = sender_filter_bytes.hex().upper() if sender_filter_bytes else ""
        sender_dev = (
            self.controller.getDeviceFromAddress(sender_filter_hex)
            if sender_filter_hex
            else None
        )

        # Resolve the routed transport. Explicit ``transport=`` wins;
        # otherwise prefer the device's network binding (multi-network
        # path); finally fall back to ``self.transport`` (single-
        # gateway default).
        routed_transport = transport
        if routed_transport is None and sender_filter_hex:
            try:
                routed_transport = self.controller.transport_for_device(sender_filter_hex)
            except Exception:
                # swallow-ok: routing helper is best-effort, fall back
                # to the singleton.
                routed_transport = None
        if routed_transport is None:
            routed_transport = self.transport
        if not routed_transport:
            return [], False
        gateway_id_filter = getattr(routed_transport, "ident_mac", None)

        # Stage 3: a unicast send requires a concrete ``gateway_id``
        # so the matcher can reject cross-transport replies. The only
        # case ``ident_mac`` is ``None`` is the brief pre-handshake
        # window before ``discover_and_open`` populates it; refusing
        # to send here is strictly safer than stamping a wildcard
        # matcher and letting a sibling transport's RX feed satisfy
        # it. Broadcast sends (``recv3 == FFFFFF``) still flow — the
        # matcher's ``sender_filter`` is ``None`` there and the
        # registry allows wildcard gateway_id.
        if sender_filter_hex and not gateway_id_filter:
            logger.warning(
                "send_and_wait_with_retries: routed transport has no "
                "ident_mac yet (pre-handshake?); refusing to send unicast "
                "opcode=0x%02X to %s",
                opcode7, sender_filter_hex,
            )
            return [], False

        opcode_name = self.opcode_name(opcode7)
        last_events: list[dict] = []
        for attempt in range(n):
            matcher = self._build_unicast_matcher(
                recv3, opcode7, per,
                discriminator=None,
                gateway_id=gateway_id_filter,
            )
            if matcher is None:
                # Opcode with no expected reply — fire-and-forget; no point
                # retrying. Mirrors the legacy ``RESP_NONE`` short-circuit.
                self.install_transport_hooks(transport=routed_transport)
                send_fn()
                return [], False
            t0 = time.monotonic()
            logger.debug(
                "send_and_wait ENTER sender=%s opcode=0x%02X(%s) attempt=%d/%d timeout=%.2fs",
                sender_filter_hex or "broadcast",
                opcode7,
                opcode_name,
                attempt + 1,
                n,
                per,
            )
            events, reason = self.send_and_match(send_fn, matcher, transport=routed_transport)
            elapsed = time.monotonic() - t0
            if events:
                logger.debug(
                    "send_and_wait EXIT  MATCHED sender=%s opcode=0x%02X(%s) reason=%s "
                    "attempt=%d/%d elapsed=%.3fs",
                    sender_filter_hex or "broadcast",
                    opcode7,
                    opcode_name,
                    reason,
                    attempt + 1,
                    n,
                    elapsed,
                )
                if sender_dev is not None:
                    try:
                        with self._state_lock():
                            sender_dev.mark_online()
                    except Exception:
                        logger.exception("RaceLink: mark_online after match raised")
                # Post-match settle (default 0.0 — diagnostic knob only).
                settle = float(getattr(self, "post_match_settle_s", 0.0) or 0.0)
                if settle > 0.0:
                    time.sleep(settle)
                return events, True
            logger.debug(
                "send_and_wait EXIT  TIMEOUT sender=%s opcode=0x%02X(%s) reason=%s "
                "attempt=%d/%d elapsed=%.3fs",
                sender_filter_hex or "broadcast",
                opcode7,
                opcode_name,
                reason,
                attempt + 1,
                n,
                elapsed,
            )
            last_events = events
            if attempt < n - 1 and delay > 0.0:
                time.sleep(delay)
        logger.debug(
            "send_and_wait_with_retries: exhausted %d attempts (opcode=0x%02X, per=%.2fs)",
            n,
            opcode7,
            per,
        )
        return last_events, False


    @staticmethod
    def compute_collect_max_timeout(
        expected: int,
        *,
        base_s: float = rf_timing.COLLECT_BASE_S,
        per_device_s: float = rf_timing.COLLECT_PER_DEVICE_S,
        ceiling_s: float = rf_timing.COLLECT_MAX_CEILING_S,
    ) -> float:
        """Derive a max-timeout ceiling from the expected responder count.

        ``base_s`` covers LBT/jitter + first-reply latency; ``per_device_s``
        scales with the known population. The final value is clamped to
        ``ceiling_s`` so very large groups cannot pin the server thread.
        """
        n = max(0, int(expected))
        return min(ceiling_s, base_s + n * float(per_device_s))

    def send_config(
        self,
        option,
        data0=0,
        data1=0,
        data2=0,
        data3=0,
        recv3=b"\xFF\xFF\xFF",
        wait_for_ack: bool = False,
        timeout_s: Optional[float] = None,
    ):
        transport = self.transport
        if transport is None:
            logger.warning("sendConfig: communicator not ready")
            return False if wait_for_ack else None

        recv3_hex = recv3.hex().upper() if isinstance(recv3, (bytes, bytearray)) else ""
        dev = None
        if recv3_hex and recv3_hex != "FFFFFF":
            # Locked stash — paired with ``take_pending_config`` on the RX
            # path below. See controller docstring for the threading
            # contract.
            self.controller.stash_pending_config(recv3_hex, option, data0)
            dev = self.controller.getDeviceFromAddress(recv3_hex)
            if dev and wait_for_ack:
                dev.ack_clear()

        def _send():
            transport.send_config(
                recv3=recv3,
                option=int(option) & 0xFF,
                data0=int(data0) & 0xFF,
                data1=int(data1) & 0xFF,
                data2=int(data2) & 0xFF,
                data3=int(data3) & 0xFF,
            )

        if wait_for_ack:
            if not dev:
                _send()
                return False
            per_attempt = (
                float(timeout_s)
                if timeout_s is not None
                else rf_timing.UNICAST_ATTEMPT_TIMEOUT_S
            )
            events, _ = self.send_and_wait_with_retries(
                recv3,
                LP.OPC_CONFIG,
                _send,
                per_attempt_timeout_s=per_attempt,
            )
            if not events:
                return False
            ev = events[-1]
            return bool(int(ev.get("ack_status", 1)) == 0)
        _send()
        return True

    def send_sync(self, ts24, brightness, recv3=b"\xFF\xFF\xFF", *, trigger_armed: bool = False):
        """Send an ``OPC_SYNC`` clock-tick packet.

        Stage 3 Part G: a broadcast sync (``recv3 == FFFFFF``) fans
        out across *every* attached transport so each network's
        devices receive a tick on their own radio. Unicast syncs
        (``recv3 != FFFFFF``) route through the device's bound
        transport via ``transport_for_device`` so the tick lands on
        the right radio. At N=1 attached gateway the fan-out
        collapses to a single send and behaviour is byte-identical
        to the pre-Part-G path.

        ``trigger_armed`` adds ``SYNC_FLAG_TRIGGER_ARMED`` to the
        wire body (operator-driven sync that materialises pending
        arm-on-sync state). Autosync MUST leave it ``False``.
        """
        from ..protocol.packets import SYNC_FLAG_TRIGGER_ARMED
        flags = SYNC_FLAG_TRIGGER_ARMED if trigger_armed else 0
        ts24_int = int(ts24) & 0xFFFFFF
        brightness_int = int(brightness) & 0xFF
        recv3_b = bytes(recv3 or b"")

        # Unicast: route to the device's network-bound transport.
        if recv3_b and recv3_b != b"\xFF\xFF\xFF":
            controller = getattr(self, "controller", None)
            routed = None
            if controller is not None:
                try:
                    sender_hex = recv3_b.hex().upper()
                    routed = controller.transport_for_device(sender_hex)
                except Exception:
                    # swallow-ok: routing helper is best-effort; the
                    # fallback singleton still works at N=1.
                    routed = None
            if routed is None:
                routed = self.transport
            if routed is None:
                logger.warning("sendSync: communicator not ready")
                return
            routed.send_sync(recv3=recv3_b, ts24=ts24_int,
                             brightness=brightness_int, flags=flags)
            return

        # Broadcast: fan out across every attached transport. Each
        # transport's TX-barrier serialises sends per-radio; the
        # cross-transport calls run sequentially on the caller
        # thread, but each USB-CDC write is sub-millisecond so the
        # whole fan-out is well under the LoRa airtime budget.
        transports = list(getattr(self.controller, "transports", None) or [])
        if not transports:
            primary = self.transport
            if primary is None:
                logger.warning("sendSync: communicator not ready")
                return
            transports = [primary]
        for t in transports:
            try:
                t.send_sync(recv3=recv3_b, ts24=ts24_int,
                            brightness=brightness_int, flags=flags)
            except Exception:
                # swallow-ok: one misbehaving transport must not abort
                # the sync fan-out on its siblings. The next OPC_SYNC
                # tick (autosync runs every few hundred ms) re-tries.
                logger.exception(
                    "RaceLink: send_sync failed on transport %r",
                    getattr(t, "ident_mac", None),
                )

    def send_stream(
        self,
        payload: bytes,
        groupId: Optional[int] = None,
        device=None,
        retries: int = rf_timing.STREAM_MAX_ATTEMPTS - 1,
        timeout_s: float = rf_timing.STREAM_ATTEMPT_TIMEOUT_S,
    ) -> dict[str, int]:
        transport = self.transport
        if transport is None:
            logger.warning("sendStream: communicator not ready")
            return {}

        self.install_transport_hooks()

        # For OPC_STREAM the host provides one logical payload. The gateway is
        # responsible for fragmenting it into radio packets and assigning the
        # per-packet stream control bytes.
        data = bytes(payload or b"")
        if len(data) > 128:
            raise ValueError("payload too large (max 128 bytes)")

        if device is None and groupId is None:
            raise ValueError("sendStream requires groupId or device")

        if device is None:
            assert groupId is not None  # narrowed by the guard above
            group_filter = int(groupId)
            # A6: snapshot the matching devices under the state lock so a
            # concurrent IDENTIFY append / device delete cannot raise on
            # iteration. The list comprehension materialises the result
            # immediately, so the lock can be released before the slower
            # downstream stream-send work begins.
            with self._state_lock():
                targets = [
                    dev
                    for dev in self.controller.device_repository.list()
                    if int(getattr(dev, "groupId", 0) or 0) == group_filter
                ]
        else:
            targets = [device]

        target_last3 = {mac_last3_from_hex(dev.addr) for dev in targets if dev and dev.addr}
        target_last3.discard(b"\xFF\xFF\xFF")
        expected = len(target_last3)
        if expected == 0:
            return {"expected": 0, "acked": 0}

        recv3 = b"\xFF\xFF\xFF" if device is None else mac_last3_from_hex(device.addr)
        if recv3 == b"\xFF\xFF\xFF" and device is not None:
            return {"expected": expected, "acked": 0}

        try:
            transport.drain_events(0.0)
        except Exception:
            logger.debug("RaceLink: drain_events before send_stream raised", exc_info=True)

        acked: set[bytes] = set()

        # Plan Phase C (revised): each retry iteration returns as soon as all
        # targets have ACKed, or after ``idle_timeout_s`` of silence on an
        # already-partial set, capped by a max derived from the target count.
        max_ceiling = float(timeout_s)
        max_timeout = min(
            max_ceiling,
            self.compute_collect_max_timeout(expected, ceiling_s=max_ceiling),
        )
        # Stage 3: multi-sender N-reply matchers also require a concrete
        # gateway_id (the target group lives on one network — Part-B
        # boundary enforcement guarantees this). The transport this
        # stream goes out on owns that network; use its ident_mac as
        # the matcher's filter.
        stream_gateway_id = getattr(transport, "ident_mac", None)
        if not stream_gateway_id:
            logger.warning(
                "send_stream: transport has no ident_mac yet; refusing "
                "to send to %d target(s) (recv3=%s)",
                expected, recv3.hex().upper(),
            )
            return {"expected": expected, "acked": 0}
        for attempt in range(max(0, int(retries)) + 1):
            remaining = target_last3 - acked
            if not remaining:
                break
            matcher = PendingMatcher(
                sender_filter=frozenset(remaining),
                expected_ack_of=int(LP.OPC_STREAM) & 0x7F,
                gateway_id=stream_gateway_id,
                expected_count=len(remaining),
                idle_timeout_s=rf_timing.COLLECT_IDLE_TIMEOUT_S,
                max_timeout_s=max_timeout,
            )
            replies, _reason = self.send_and_match(
                lambda: transport.send_stream(recv3=recv3, payload=data),
                matcher,
                transport=transport,
            )
            for ev in replies:
                s3 = ev.get("sender3")
                if isinstance(s3, (bytes, bytearray)):
                    acked.add(bytes(s3))
            if len(acked) >= expected:
                break
            if attempt < int(retries):
                time.sleep(rf_timing.STREAM_RETRY_DELAY_S)

        return {"expected": expected, "acked": len(acked)}

    def wait_rx_window(
        self,
        send_fn,
        collect_pred=None,
        fail_safe_s: float = 8.0,
        *,
        stop_on_match: bool = False,
    ):
        """Legacy reply-window helper (deprecated -- plan Transport Redesign D).

        The Gateway no longer drives a Timed RX window after unicast TX; it
        stays in Continuous RX. New callers should build a
        :class:`PendingMatcher` and call :meth:`send_and_match`
        — covers both unicast 1-reply and multi-sender N-reply paths.

        Batch B (2026-04-28) collapsed EV_RX_WINDOW_OPEN/CLOSED into
        EV_STATE_CHANGED; the "window closed" signal is now an
        EV_STATE_CHANGED transition out of the RX_WINDOW state byte. This
        helper detects that transition while remaining backwards-compatible
        with its (collected, got_closed) return tuple.
        """
        if not self.transport:
            return [], False

        transport = self.transport
        collected = []
        got_closed = False

        def _is_window_closed_transition(ev: dict) -> bool:
            # EV_STATE_CHANGED away from RX_WINDOW = the legacy "closed".
            if ev.get("type") != EV_STATE_CHANGED:
                return False
            new_state = int(ev.get("state_byte", -1))
            return new_state != GATEWAY_STATE_RX_WINDOW

        if hasattr(transport, "add_listener") and hasattr(transport, "remove_listener"):
            closed_ev = threading.Event()

            def _cb(ev: dict):
                nonlocal got_closed
                try:
                    if not isinstance(ev, dict):
                        return
                    if _is_window_closed_transition(ev):
                        got_closed = True
                        closed_ev.set()
                        return
                    if collect_pred and collect_pred(ev):
                        collected.append(ev)
                        if stop_on_match:
                            closed_ev.set()
                except Exception:
                    logger.exception("RaceLink: reply-collector callback raised")

            transport.add_listener(_cb)
            try:
                send_fn()
                closed_ev.wait(timeout=float(fail_safe_s))
            finally:
                try:
                    transport.remove_listener(_cb)
                except Exception:
                    logger.debug("RaceLink: remove_listener failed during cleanup", exc_info=True)
            return collected, got_closed

        send_fn()
        t_end = time.time() + float(fail_safe_s)
        while time.time() < t_end:
            for ev in transport.drain_events(timeout_s=0.1):
                if _is_window_closed_transition(ev):
                    got_closed = True
                    return collected, got_closed
                if collect_pred and collect_pred(ev):
                    collected.append(ev)
                    if stop_on_match:
                        return collected, got_closed
        return collected, got_closed

    def query_state(self, *, timeout_s: float = 0.5) -> dict:
        """Send GW_CMD_STATE_REQUEST and wait for the matching EV_STATE_REPORT.

        Returns a dict with the same shape the SSE layer broadcasts:

            {
                "state": "IDLE" | "TX" | "RX_WINDOW" | "RX" | "ERROR" | "UNKNOWN",
                "state_byte": int,
                "state_metadata_ms": int,
                "ok": bool,            # True iff a STATE_REPORT actually arrived
            }

        Used at startup (host-side seed of the master pill before any
        spontaneous EV_STATE_CHANGED would have fired) and from the master-
        pill ↻ refresh button. ``timeout_s`` is short by design — the round-
        trip is a USB write + USB read with no LoRa airtime; 500 ms is
        generous.
        """
        transport = self.transport
        if transport is None:
            return {
                "ok": False,
                "state": "UNKNOWN",
                "state_byte": GATEWAY_STATE_UNKNOWN,
                "state_metadata_ms": 0,
            }

        replied = threading.Event()
        result: dict = {}

        def _cb(ev: dict):
            try:
                if not isinstance(ev, dict):
                    return
                if ev.get("type") != EV_STATE_REPORT:
                    return
                result["state"] = ev.get("state") or GATEWAY_STATE_NAME.get(
                    int(ev.get("state_byte", GATEWAY_STATE_UNKNOWN)), "UNKNOWN",
                )
                result["state_byte"] = int(ev.get("state_byte", GATEWAY_STATE_UNKNOWN))
                result["state_metadata_ms"] = int(ev.get("state_metadata_ms", 0) or 0)
                replied.set()
            except Exception:
                logger.debug("query_state callback raised", exc_info=True)

        try:
            transport.add_listener(_cb)
        except Exception:
            logger.debug("query_state: add_listener failed", exc_info=True)
            # Fall through to write-and-snapshot — better than failing hard.

        try:
            ok_write = True
            try:
                send = getattr(transport, "send_state_request", None)
                if callable(send):
                    ok_write = bool(send())
            except Exception:
                logger.debug("query_state: send_state_request raised", exc_info=True)
                ok_write = False

            if ok_write:
                replied.wait(timeout=float(timeout_s))
        finally:
            try:
                transport.remove_listener(_cb)
            except Exception:
                logger.debug("query_state: remove_listener failed", exc_info=True)

        if result:
            result["ok"] = True
            return result

        # Fallback: report the transport's last-known state. Better than
        # nothing — the operator at least sees whatever the pill mirror has.
        snap = getattr(transport, "gateway_state_snapshot", None)
        if callable(snap):
            try:
                snap_obj = snap()
                if isinstance(snap_obj, dict):
                    base: dict = dict(snap_obj)
                    base["ok"] = False
                    return base
            except Exception:
                logger.debug("query_state: snapshot raised", exc_info=True)
        return {
            "ok": False,
            "state": "UNKNOWN",
            "state_byte": GATEWAY_STATE_UNKNOWN,
            "state_metadata_ms": 0,
        }

    # ---- Gateway RF config (USB-CDC round-trip, no LoRa) -----------------

    def query_gateway_rf_config(
        self,
        *,
        timeout_s: float = 0.5,
        transport=None,
    ) -> dict:
        """Send GW_CMD_GET_RF_CONFIG and wait for the matching EV_RF_CHANGED.

        Returns:
            ``{"ok": True, "rf_config": {...}}`` on success;
            ``{"ok": False, "error": "..."}`` on transport unavailable or
            timeout. The success ``rf_config`` shape is the wire-format
            P_RfConfig dict (freq_hz / bw_khz_x10 / sf / cr_den /
            sync_word / tx_power_dbm / preamble).

        The round-trip is a USB write + USB read; 500 ms is generous.

        ``transport`` (Stage 3 Part D): query a specific transport
        instance instead of the controller's primary slot. Used by
        the bind service to evaluate every newly-attached transport
        in turn — at N=1 the default of ``self.transport`` is the
        right transport anyway, but multi-gateway deployments need
        to address the right one.
        """
        if transport is None:
            transport = self.transport
        if transport is None:
            return {"ok": False, "error": "transport unavailable"}

        replied = threading.Event()
        result: dict = {}

        def _cb(ev: dict):
            try:
                if not isinstance(ev, dict):
                    return
                if ev.get("type") != EV_RF_CHANGED:
                    return
                cfg = ev.get("rf_config")
                if isinstance(cfg, dict):
                    result["rf_config"] = cfg
                    result["reason"] = int(ev.get("reason", RF_CHANGE_OK))
                    result["reason_name"] = ev.get(
                        "reason_name",
                        RF_CHANGE_REASON_NAME.get(result["reason"], "unknown"),
                    )
                    replied.set()
            except Exception:
                logger.debug("query_gateway_rf_config callback raised", exc_info=True)

        try:
            transport.add_listener(_cb)
        except Exception:
            logger.debug("query_gateway_rf_config: add_listener failed", exc_info=True)

        try:
            ok_write = True
            try:
                send = getattr(transport, "send_get_rf_config", None)
                if callable(send):
                    ok_write = bool(send())
                else:
                    ok_write = False
            except Exception:
                logger.debug("query_gateway_rf_config: send raised", exc_info=True)
                ok_write = False

            if ok_write:
                replied.wait(timeout=float(timeout_s))
        finally:
            try:
                transport.remove_listener(_cb)
            except Exception:
                logger.debug("query_gateway_rf_config: remove_listener failed", exc_info=True)

        if "rf_config" in result:
            result["ok"] = True
            return result

        return {"ok": False, "error": "no reply within timeout"}

    def set_gateway_rf_config(
        self,
        rf_config: dict,
        *,
        persist: bool = True,
        timeout_s: float = 1.0,
        transport=None,
    ) -> dict:
        """Send GW_CMD_SET_RF_CONFIG and wait for the matching EV_RF_CHANGED.

        ``persist=True`` writes NVS and reboots the gateway (the EV is
        emitted ~100 ms BEFORE the reboot so we still catch it). With
        ``persist=False`` the gateway live-reconfigures and stays up.

        ``transport`` (Stage 3 Part E): address a specific gateway
        instance instead of the controller's primary slot. The
        migration engine targets one network at a time and routes the
        switch via the bound transport.

        Returns ``{"ok": bool, "reason": int, "reason_name": str,
                   "rf_config": {...} | None}``. ``ok`` is True only for
        ``reason == RF_CHANGE_OK``; any rejection (range / NVS / CRC)
        sets ``ok=False`` and carries the still-active config in
        ``rf_config`` (per the firmware contract).
        """
        if transport is None:
            transport = self.transport
        if transport is None:
            return {"ok": False, "error": "transport unavailable"}

        replied = threading.Event()
        result: dict = {}

        def _cb(ev: dict):
            try:
                if not isinstance(ev, dict):
                    return
                if ev.get("type") != EV_RF_CHANGED:
                    return
                result["reason"] = int(ev.get("reason", 0))
                result["reason_name"] = ev.get(
                    "reason_name",
                    RF_CHANGE_REASON_NAME.get(result["reason"], "unknown"),
                )
                result["rf_config"] = ev.get("rf_config")
                replied.set()
            except Exception:
                logger.debug("set_gateway_rf_config callback raised", exc_info=True)

        try:
            transport.add_listener(_cb)
        except Exception:
            logger.debug("set_gateway_rf_config: add_listener failed", exc_info=True)

        try:
            ok_write = True
            try:
                send = getattr(transport, "send_set_rf_config", None)
                if callable(send):
                    ok_write = bool(send(rf_config, persist=persist))
                else:
                    ok_write = False
            except Exception:
                logger.debug("set_gateway_rf_config: send raised", exc_info=True)
                ok_write = False

            if ok_write:
                replied.wait(timeout=float(timeout_s))
        finally:
            try:
                transport.remove_listener(_cb)
            except Exception:
                logger.debug("set_gateway_rf_config: remove_listener failed", exc_info=True)

        if "reason" in result:
            result["ok"] = (result["reason"] == RF_CHANGE_OK)
            return result

        return {"ok": False, "error": "no reply within timeout"}

    # ---- Node RF config (LoRa OPC_RF_CONFIG / OPC_GET_RF_CONFIG) ---------

    def set_node_rf_config(
        self,
        mac: str,
        rf_config: dict,
        *,
        timeout_s: Optional[float] = None,
        transport=None,
    ) -> dict:
        """Push a new ``P_RfConfig`` to a node over LoRa (OPC_RF_CONFIG).

        The node validates, persists to NVS, ACKs (``ACK_OK`` or
        ``ACK_BAD_LEN`` / ``ACK_ERROR``), then reboots ~50 ms later
        onto the new RF settings. The reboot drops the link, which is
        expected — the operator-facing outcome is the ACK, not a
        post-reboot heartbeat.

        ``transport`` (Stage 3 Part E): route the push via a specific
        gateway transport, e.g. when the migration engine is talking
        through a particular network's gateway. The default
        ``transport_for_device(mac)`` resolution covers single-gateway
        callers; multi-network callers pass it explicitly.

        Returns ``{"ok": bool, "ack_status": int | None, "error": str?}``.
        ``ok`` is True iff ``ack_status == 0`` (ACK_OK). Range / NVS
        rejections return ``ok=False`` with the FW's ack_status; transport
        timeouts return ``ok=False`` with ``error="timeout"``.
        """
        # ``transport`` chooses both the wire-send transport and the
        # routed matcher. Falling back to ``self.transport`` mirrors
        # the pre-Stage-3 behaviour at N=1.
        if transport is None:
            # Prefer the device's network-bound transport so the actual
            # ``send_rf_config`` goes via the right radio.
            controller = getattr(self, "controller", None)
            if controller is not None:
                try:
                    transport = controller.transport_for_device(
                        str(mac or "").strip().upper()
                    )
                except Exception:
                    # swallow-ok: routing helper is best-effort; the
                    # fallback singleton still works at N=1.
                    transport = None
        if transport is None:
            transport = self.transport
        if transport is None:
            return {"ok": False, "error": "transport unavailable"}

        mac_str = str(mac or "").strip().upper()
        if len(mac_str) != 12:
            return {"ok": False, "error": "mac must be 12 hex chars"}

        recv3 = mac_last3_from_hex(mac_str)
        if recv3 == b"\xFF\xFF\xFF":
            # Defence in depth: the firmware also rejects broadcast for
            # OPC_RF_CONFIG, but we should never even hit the wire with
            # a broadcast send — it would brick every reachable node.
            return {"ok": False, "error": "broadcast forbidden for OPC_RF_CONFIG"}

        # Clear any stale ACK state on the device so the caller's `ok`
        # signal isn't a stale read from a previous send.
        dev = self.controller.getDeviceFromAddress(mac_str)
        if dev is not None:
            try:
                dev.ack_clear()
            except Exception:
                # swallow-ok: best-effort; ack_clear only fails on
                # malformed device records that other paths already
                # log.
                logger.debug("set_node_rf_config: ack_clear failed for %s", mac_str, exc_info=True)

        def _send():
            transport.send_rf_config(recv3=recv3, rf_config=rf_config)

        per_attempt = (
            float(timeout_s)
            if timeout_s is not None
            else rf_timing.UNICAST_ATTEMPT_TIMEOUT_S
        )
        events, _ = self.send_and_wait_with_retries(
            recv3,
            LP.OPC_RF_CONFIG,
            _send,
            per_attempt_timeout_s=per_attempt,
            transport=transport,
        )
        if not events:
            return {"ok": False, "error": "timeout"}
        ev = events[-1]
        ack_status = int(ev.get("ack_status", 1))
        return {
            "ok": ack_status == 0,
            "ack_status": ack_status,
        }

    def query_node_rf_config(
        self,
        mac: str,
        *,
        timeout_s: Optional[float] = None,
    ) -> dict:
        """Read back a node's currently active P_RfConfig over LoRa.

        Sends OPC_GET_RF_CONFIG; the node replies with a 12 B P_RfConfig
        body (decoded by :func:`parse_reply_event`). Returns
        ``{"ok": bool, "rf_config": {...}}`` on success.
        """
        transport = self.transport
        if transport is None:
            return {"ok": False, "error": "transport unavailable"}

        mac_str = str(mac or "").strip().upper()
        if len(mac_str) != 12:
            return {"ok": False, "error": "mac must be 12 hex chars"}

        recv3 = mac_last3_from_hex(mac_str)
        if recv3 == b"\xFF\xFF\xFF":
            return {"ok": False, "error": "broadcast forbidden for OPC_GET_RF_CONFIG"}

        def _send():
            transport.send_get_rf_config_to_node(recv3=recv3)

        per_attempt = (
            float(timeout_s)
            if timeout_s is not None
            else rf_timing.UNICAST_ATTEMPT_TIMEOUT_S
        )
        events, _ = self.send_and_wait_with_retries(
            recv3,
            LP.OPC_GET_RF_CONFIG,
            _send,
            per_attempt_timeout_s=per_attempt,
        )
        if not events:
            return {"ok": False, "error": "timeout"}
        ev = events[-1]
        rf_config = ev.get("rf_config")
        if not isinstance(rf_config, dict):
            return {"ok": False, "error": "malformed reply", "raw": ev.get("body_raw")}
        return {"ok": True, "rf_config": rf_config}

    def opcode_name(self, opcode7: int) -> str:
        return protocol_opcode_name(int(opcode7) & 0x7F)

    def log_transport_reply(self, ev: dict) -> None:
        try:
            opc = int(ev.get("opc", -1)) & 0x7F
        except Exception:
            # swallow-ok: malformed event in a best-effort log helper
            return

        sender3_hex = self.controller._to_hex_str(ev.get("sender3")) or "??????"

        if opc == int(LP.OPC_ACK):
            ack_of = ev.get("ack_of")
            ack_status = ev.get("ack_status")
            ack_seq = ev.get("ack_seq")
            if ack_of is None or ack_status is None:
                return
            ack_name = self.opcode_name(int(ack_of))
            logger.debug("ACK from %s: ack_of=%s (%s) status=%s seq=%s", sender3_hex, int(ack_of), ack_name, int(ack_status), ack_seq)
            return

        if opc == int(LP.OPC_STATUS) and ev.get("reply") == "STATUS_REPLY":
            logger.debug(
                "STATUS from %s: flags=0x%02X cfg=0x%02X effect=%s bri=%s vbat=%s rssi=%s snr=%s host_rssi=%s host_snr=%s",
                sender3_hex,
                int(ev.get("flags", 0) or 0) & 0xFF,
                int(ev.get("configByte", 0) or 0) & 0xFF,
                ev.get("effectId"),
                ev.get("brightness"),
                ev.get("vbat_mV"),
                ev.get("node_rssi"),
                ev.get("node_snr"),
                ev.get("host_rssi"),
                ev.get("host_snr"),
            )
            return

        if opc == int(LP.OPC_DEVICES) and ev.get("reply") == "IDENTIFY_REPLY":
            mac6 = ev.get("mac6")
            mac12 = bytes(mac6).hex().upper() if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6 else None
            dev_type = ev.get("caps")
            dtype_name = get_dev_type_info(dev_type).get("name")
            logger.debug(
                "IDENTIFY from %s: mac=%s group=%s ver=%s dev_type=%s (%s) host_rssi=%s host_snr=%s",
                sender3_hex,
                mac12 or sender3_hex,
                ev.get("groupId"),
                ev.get("version"),
                dev_type,
                dtype_name,
                ev.get("host_rssi"),
                ev.get("host_snr"),
            )
            return

        if ev.get("reply"):
            logger.debug("RX %s from %s (opc=0x%02X)", ev.get("reply"), sender3_hex, opc)

    def log_state_event(self, ev: dict) -> None:
        """Log a Batch-B EV_STATE_CHANGED / EV_STATE_REPORT event for diagnostics.

        Replaces the pre-Batch-B ``log_rx_window_event``: the window-open /
        window-closed pair is now expressed as transitions of the gateway's
        single state byte. RX_WINDOW carries ``state_metadata_ms`` (the
        ``min_ms`` window size); other states carry 0.
        """
        t = ev.get("type")
        if t not in (EV_STATE_CHANGED, EV_STATE_REPORT):
            return
        state_name = ev.get("state") or GATEWAY_STATE_NAME.get(int(ev.get("state_byte", -1)), "UNKNOWN")
        meta_ms = int(ev.get("state_metadata_ms", 0) or 0)
        if state_name == "RX_WINDOW":
            logger.debug("Gateway state -> RX_WINDOW (min_ms=%s)", meta_ms)
        else:
            logger.debug("Gateway state -> %s", state_name)

    def handle_ack_event(self, ev: dict) -> None:
        try:
            sender3_hex = self.controller._to_hex_str(ev.get("sender3"))
            with self._state_lock():
                dev = self.controller.getDeviceFromAddress(sender3_hex) if sender3_hex else None
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
                    # Locked pop — paired with ``stash_pending_config`` on
                    # the TX path. ``_apply_config_update`` runs outside
                    # the pending-config lock so a slow ConfigService
                    # callback cannot delay the next stash.
                    pending = self.controller.take_pending_config(sender3_hex)
                    if pending:
                        self.controller._apply_config_update(dev, pending.get("option", 0), pending.get("data0", 0))

        except Exception:
            logger.exception("ACK handling failed")

    def install_transport_hooks(self, transport=None) -> None:
        """Idempotently wire RX + TX listeners onto ``transport``.

        Stage 2 Part 3: ``transport`` defaults to ``self.transport``
        (the controller's primary slot) for full backwards-compat. When
        the controller holds multiple transports each one must be
        wired separately; callers that have already routed via
        ``transport_for_device`` pass the resolved transport through
        so its RX events also reach this service's dispatcher.

        Tracked per-transport via ``controller._transport_hooks_installed_for``
        keyed by ``id(transport)`` — the new identity replaces the
        prior single-bool flag that blocked re-installing onto
        sibling transports.
        """
        if transport is None:
            transport = self.transport
        if not transport:
            return
        key = id(transport)
        installed_set = self.controller._transport_hooks_installed_for
        if key in installed_set:
            return

        try:
            if hasattr(transport, "add_listener"):
                transport.add_listener(self.on_transport_event)
            else:
                prev = getattr(transport, "on_event", None)

                def _mux(ev):
                    try:
                        self.on_transport_event(ev)
                    except Exception:
                        logger.exception("RaceLink: gateway service transport handler raised")
                    if prev:
                        try:
                            prev(ev)
                        except Exception:
                            logger.exception("RaceLink: downstream on_event handler raised")

                transport.on_event = _mux
        except Exception:
            logger.exception("RaceLink: failed to install transport RX listener")

        try:
            if hasattr(transport, "add_tx_listener"):
                transport.add_tx_listener(self.on_transport_tx)
        except Exception:
            logger.exception("RaceLink: failed to install transport TX listener")

        installed_set.add(key)

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
                rule = protocol_rules.find_rule(opcode7)
            except Exception:
                # swallow-ok: unknown opcode treated as "no rule" -> skip TX tracking
                rule = None
            if not rule:
                return

            if int(request_direction(opcode7)) != int(protocol_rules.DIR_M2N):
                return

            policy = int(response_policy(opcode7))
            if policy == int(protocol_rules.RESP_NONE):
                return

            dev = self.controller.getDeviceFromAddress(recv3_b.hex().upper())
            if not dev:
                return

            # A5: stash via the controller helper so the TX-listener
            # write is atomic against the RX-reader's match/clear path.
            # Stage 2 Part 3: keyed per-gateway so two transports cannot
            # overwrite each other's in-flight expectations. The
            # transport layer tags every TX event with ``gateway_id``
            # (its ident_mac) in ``_emit_tx``.
            self.controller.set_pending_expect(
                dev=dev,
                rule=rule,
                opcode7=opcode7,
                sender_last3=(dev.addr or "").upper()[-6:],
                ts=time.time(),
                gateway_id=ev.get("gateway_id"),
            )
        except Exception:
            logger.exception("RaceLink: TX hook failed")

    def on_transport_event(self, ev: dict) -> None:
        try:
            if not isinstance(ev, dict):
                return

            t = ev.get("type")

            if t == EV_ERROR:
                reason = str(ev.get("data") or "unknown error")
                self.controller.ready = False
                now = time.time()
                if (now - self.controller._last_error_notify_ts) > 2:
                    self.controller._last_error_notify_ts = now
                    try:
                        host_api = getattr(self.controller, "_host_api", None)
                        ui = getattr(host_api, "ui", None) if host_api is not None else None
                        notify = getattr(ui, "message_notify", None) if ui is not None else None
                        translator = getattr(host_api, "__", None) if host_api is not None else None
                        if callable(notify):
                            template = "RaceLink Gateway disconnected: {}"
                            if callable(translator):
                                translated = translator(template)
                                template = translated if isinstance(translated, str) else template
                            notify(template.format(reason))
                    except Exception:
                        logger.exception("RaceLink: failed to notify UI about disconnect")
                self.schedule_reconnect(reason)
                return

            if t in (EV_STATE_CHANGED, EV_STATE_REPORT):
                self.log_state_event(ev)
                # Post-Batch-B: pending unicast requests time out via the
                # registry's wall-clock deadline rather than via an explicit
                # RX_WINDOW_CLOSED event. We still trigger the timeout-style
                # pending_window_closed sweep on the RX_WINDOW -> non-window
                # transition so the legacy "missing reply" path still fires
                # for the broadcast-fallback callers that depend on it.
                state_byte = int(ev.get("state_byte", -1))
                if state_byte != GATEWAY_STATE_RX_WINDOW:
                    self.pending_window_closed(ev)
                return

            if t == EV_TX_REJECTED:
                # Diagnostic surface only — _send_m2n's outcome wait already
                # received this NACK and converted it to a SendOutcome.REJECTED.
                # Logging keeps the failure observable for non-_send_m2n paths
                # (e.g. orphan NACKs from gateway-internal auto-sync).
                logger.debug(
                    "EV_TX_REJECTED type=0x%02X opc=0x%02X reason=%s",
                    int(ev.get("type_full", 0) or 0),
                    int(ev.get("opc", 0) or 0),
                    ev.get("reason_name") or ev.get("reason"),
                )
                return

            if t == EV_TX_DONE:
                # Post-redesign diagnostic: when an inbound reply never
                # arrives, knowing whether the Gateway ever emitted TX_DONE
                # distinguishes "CAD/LBT stuck" from "RF ACK lost".
                logger.debug(
                    "EV_TX_DONE last_len=%s ts=%.3f", ev.get("last_len"), ev.get("ts", time.time())
                )
                return

            opc = ev.get("opc")
            if opc is None:
                # Any unknown event byte (e.g. EV_IDLE 0xF4) -- still log so
                # we can see the full USB event stream during diagnostics.
                if t is not None:
                    logger.debug("transport event type=0x%02X data=%r", int(t), ev.get("data"))
                return

            self.log_transport_reply(ev)

            # Unified matcher routing: any registered PendingMatcher whose
            # filters accept this event is signalled here. Unblocks the
            # blocking caller in ``send_and_match``; the remainder of this
            # handler then updates device state for the same event so the
            # unsolicited pipeline keeps working.
            #
            # Stage 2 Part 3: dispatch into the per-gateway registry
            # whose ``gateway_id`` matches the event's source transport,
            # then also offer it to the wildcard (None) registry so any
            # legacy unrouted matcher still sees it. Concrete matchers
            # in foreign registries never see this event — that is the
            # whole point of the split.
            try:
                ev_gw = ev.get("gateway_id")
                if ev_gw is not None:
                    routed_reg = self._pending_registries.get(ev_gw)
                    if routed_reg is not None:
                        routed_reg.try_match(ev)
                self._pending_registries[None].try_match(ev)
            except Exception:
                logger.exception("RaceLink: pending-registry match raised")

            if int(opc) == int(LP.OPC_ACK):
                self.handle_ack_event(ev)
            elif int(opc) == int(LP.OPC_STATUS) and ev.get("reply") == "STATUS_REPLY":
                sender3_hex = self.controller._to_hex_str(ev.get("sender3"))
                with self._state_lock():
                    dev = self.controller.getDeviceFromAddress(sender3_hex) if sender3_hex else None
                    if dev:
                        dev.update_from_status(
                            ev.get("flags"),
                            ev.get("configByte"),
                            ev.get("effectId"),
                            ev.get("brightness"),
                            ev.get("vbat_mV"),
                            ev.get("node_rssi"),
                            ev.get("node_snr"),
                            ev.get("host_rssi"),
                            ev.get("host_snr"),
                        )
            elif int(opc) == int(LP.OPC_GET_CONFIG) and ev.get("reply") == "GET_CONFIG_REPLY":
                # Pending-registry match above already handed the parsed
                # event to the waiting ``ConfigService.read_config``
                # caller; no further state mutation here. The host
                # deliberately does NOT auto-update ``dev.specials`` —
                # the operator drives import via the dialog's
                # divergence-resolution buttons (POST
                # /api/specials/config/import).
                pass
            elif int(opc) == int(LP.OPC_DEVICES) and ev.get("reply") == "IDENTIFY_REPLY":
                mac6 = ev.get("mac6")
                if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                    mac12 = bytes(mac6).hex().upper()
                    with self._state_lock():
                        dev = self.controller.getDeviceFromAddress(mac12)
                        is_known_device = dev is not None
                        if not dev:
                            dev_type = ev.get("caps", 0)
                            dev = create_device(addr=mac12, dev_type=int(dev_type or 0), name=default_device_name(mac12))
                            self.controller.device_repository.append(dev)

                        dev.update_from_identify(
                            ev.get("version"),
                            ev.get("caps"),
                            ev.get("groupId"),
                            mac6,
                            ev.get("host_rssi"),
                            ev.get("host_snr"),
                        )
                    # Signal any ``wait_for_identify`` waiter for this MAC.
                    # Set before ``_restore_known_device_group`` so OTA can
                    # immediately move on to waiting for the auto-restore
                    # worker that the next line is about to spawn.
                    with self._identify_events_lock:
                        identify_event = self._identify_events.get(mac12)
                        if identify_event is None:
                            identify_event = threading.Event()
                            self._identify_events[mac12] = identify_event
                    identify_event.set()
                    self._restore_known_device_group(
                        dev,
                        reported_group=ev.get("groupId"),
                        is_known_device=is_known_device,
                        gateway_id=ev.get("gateway_id"),
                    )

            self.pending_try_match(ev)
        except Exception:
            logger.exception("RaceLink: RX hook failed")

    def _restore_known_device_group(
        self,
        dev,
        *,
        reported_group,
        is_known_device: bool,
        gateway_id: Optional[str] = None,
    ) -> None:
        if not is_known_device or not dev:
            return

        try:
            node_group = int(reported_group or 0) & 0xFF
        except Exception:
            # swallow-ok: malformed groupId in IDENTIFY reply -> treat as "unconfigured"
            node_group = 0

        if node_group != 0:
            return

        if self._is_discovery_active():
            return

        try:
            stored_group = int(getattr(dev, "groupId", 0) or 0) & 0xFF
        except Exception:
            logger.debug("RaceLink: unreadable stored groupId on %r", getattr(dev, "addr", "?"), exc_info=True)
            stored_group = 0

        try:
            group_count = len(self.controller.group_repository.list())
        except Exception:
            logger.debug("RaceLink: group_repository length unavailable", exc_info=True)
            group_count = 0

        if stored_group >= group_count:
            stored_group = 0
            try:
                dev.groupId = 0
            except Exception:
                logger.debug("RaceLink: could not reset invalid groupId on %r", getattr(dev, "addr", "?"), exc_info=True)

        if stored_group == node_group:
            return

        mac = str(getattr(dev, "addr", "") or "").upper()
        if not mac:
            return
        # Stage 2 Part 3: cooldown keyed by (gateway_id, mac) so an
        # auto-restore on gateway-A doesn't suppress a parallel
        # restore on gateway-B for the same MAC (degenerate but
        # logically possible after a hardware swap). At N=1 the key
        # collapses to ``(None, mac)`` and is functionally identical
        # to the prior MAC-only key.
        if self._auto_reassign_suppressed(mac, gateway_id=gateway_id):
            return

        # Plan P2-6: wait for the ACK, but do it off the transport thread so
        # blocking here never stalls reply collection. A 3s timeout bounds
        # the worker; on failure we mark the device offline so the UI shows
        # the mismatch instead of silently masking it.
        self._mark_auto_reassign(mac, gateway_id=gateway_id)
        self._spawn_auto_reassign_worker(dev, stored_group=stored_group)

    def _spawn_auto_reassign_worker(self, dev, *, stored_group: int) -> None:
        """Submit ``setNodeGroupId(wait_for_ack=True)`` to the bounded
        auto-restore executor. The executor caps concurrency at 8
        workers (see ``__init__``) and reuses threads across submits."""
        def _worker():
            try:
                ok = self.controller.setNodeGroupId(
                    dev, forceSet=True, wait_for_ack=True
                )
            except Exception:
                logger.exception(
                    "RaceLink: auto-restore SET_GROUP raised for %s (target group=%s)",
                    getattr(dev, "addr", "?"),
                    stored_group,
                )
                return
            if ok is False:
                logger.warning(
                    "RaceLink: auto-restore SET_GROUP not ACKed for %s (target group=%s)",
                    getattr(dev, "addr", "?"),
                    stored_group,
                )
                try:
                    with self._state_lock():
                        dev.mark_offline("Auto-restore SET_GROUP timeout")
                except Exception:
                    logger.exception(
                        "RaceLink: failed to mark %s offline after auto-restore timeout",
                        getattr(dev, "addr", "?"),
                    )

        mac_key = self._normalize_mac_key(getattr(dev, "addr", "") or "")
        with self._auto_reassign_lock:
            # Prune completed futures so the in-flight list stays
            # bounded between submits. The executor itself manages
            # the worker threads; this list is purely for the test
            # join-hook below.
            self._auto_restore_futures = [
                f for f in self._auto_restore_futures if not f.done()
            ]
            # Drop completed per-MAC entries by the same rule. A future
            # tied to a MAC stays under that key until a new worker for
            # the same MAC replaces it.
            stale = [m for m, f in self._auto_restore_futures_by_mac.items() if f.done()]
            for m in stale:
                self._auto_restore_futures_by_mac.pop(m, None)
            try:
                fut = self._auto_restore_executor.submit(_worker)
            except RuntimeError:
                # swallow-ok: executor was shut down (process is
                # exiting). Nothing else to do — the auto-restore
                # is best-effort.
                logger.debug(
                    "auto-restore executor refused submit (shut down?)",
                    exc_info=True,
                )
                return
            self._auto_restore_futures.append(fut)
            if mac_key:
                self._auto_restore_futures_by_mac[mac_key] = fut

    def _join_auto_restore_workers(self, timeout: float = 5.0) -> None:
        """Wait for in-flight auto-restore workers to complete.

        Test hook — production code never needs to join (the workers
        are best-effort). Iterates over a snapshot of the futures so
        a concurrent submit doesn't change what we're waiting on.
        """
        with self._auto_reassign_lock:
            futs = list(self._auto_restore_futures)
        for fut in futs:
            try:
                fut.result(timeout=timeout)
            except Exception:
                # swallow-ok: test hook just wants a deterministic
                # "all in-flight work has finished" barrier.
                # The worker itself logs any real failure.
                logger.debug(
                    "auto-restore worker future raised in test join",
                    exc_info=True,
                )

    @staticmethod
    def _normalize_mac_key(mac: str) -> str:
        return "".join(c for c in str(mac or "") if c.isalnum()).upper()

    def clear_identify(self, mac: str) -> None:
        """Forget any past IDENTIFY_REPLY for ``mac`` so a subsequent
        :meth:`wait_for_identify` only resolves on a *new* reply.

        Used by the OTA workflow before each AP-Open: we want the post-
        reboot identify to gate the next iteration, not a stale event
        from earlier in the session.
        """
        key = self._normalize_mac_key(mac)
        if not key:
            return
        with self._identify_events_lock:
            ev = self._identify_events.get(key)
        if ev is not None:
            ev.clear()

    def wait_for_identify(self, mac: str, timeout_s: float) -> bool:
        """Block until an IDENTIFY_REPLY for ``mac`` arrives or
        ``timeout_s`` elapses. Returns ``True`` on identify, ``False``
        on timeout. Safe to call after ``clear_identify`` or before any
        IDENTIFY has ever arrived — a fresh ``threading.Event`` is
        created on first use.
        """
        key = self._normalize_mac_key(mac)
        if not key:
            return False
        with self._identify_events_lock:
            ev = self._identify_events.get(key)
            if ev is None:
                ev = threading.Event()
                self._identify_events[key] = ev
        return ev.wait(timeout=timeout_s)

    def wait_for_auto_restore(self, mac: str, timeout_s: float) -> bool:
        """Block until the in-flight auto-restore worker for ``mac``
        completes, or ``timeout_s`` elapses. Returns ``True`` when the
        worker finished (regardless of ACK result — the worker itself
        logs failures), ``False`` on timeout, and ``True`` immediately
        if no worker is currently in-flight for that MAC.

        OTA uses this after :meth:`wait_for_identify` so the next
        device's AP-Open is not started while ``setNodeGroupId`` is
        still on the radio for the previous device.
        """
        key = self._normalize_mac_key(mac)
        if not key:
            return True
        with self._auto_reassign_lock:
            fut = self._auto_restore_futures_by_mac.get(key)
        if fut is None or fut.done():
            return True
        try:
            fut.result(timeout=timeout_s)
            return True
        except Exception:
            # swallow-ok: ``concurrent.futures.TimeoutError`` is the
            # timeout case; any other exception is a worker-side
            # failure that the worker itself already logged in
            # ``_spawn_auto_reassign_worker``. Either way the
            # caller's "is the radio still busy for this MAC?"
            # question is answered: ``False`` means still busy.
            return fut.done()

    def shutdown(self) -> None:
        """Release the auto-restore executor. Safe to call multiple
        times. Called from ``RaceLink_Host.shutdown``; tests can also
        invoke this to ensure the pool is gone before the test exits.
        """
        try:
            self._auto_restore_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            # swallow-ok: shutdown is best-effort cleanup. ``shutdown``
            # raises only if the executor is already in a broken
            # state; nothing useful to do at that point.
            logger.debug("auto-restore executor shutdown raised", exc_info=True)

    def _is_discovery_active(self) -> bool:
        checker = getattr(self.controller, "is_discovery_active", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:
            # swallow-ok: best-effort query; when in doubt we assume "no discovery"
            return False

    def _auto_reassign_suppressed(self, mac: str, *, gateway_id: Optional[str] = None) -> bool:
        # Stage 2 Part 3: cooldown key is ``(gateway_id, mac)`` so the
        # same MAC tracked under two transports has independent
        # cooldowns. ``gateway_id=None`` at N=1 preserves Stage-2
        # behaviour byte-for-byte (all entries share the same first
        # tuple element).
        key = (gateway_id, mac)
        now = time.time()
        with self._auto_reassign_lock:
            self._prune_auto_reassign_cache_locked(now)
            last_ts = float(self._auto_reassign_recent.get(key, 0.0) or 0.0)
        return (now - last_ts) < float(self._auto_reassign_cooldown_s)

    def _mark_auto_reassign(self, mac: str, *, gateway_id: Optional[str] = None) -> None:
        key = (gateway_id, mac)
        with self._auto_reassign_lock:
            self._auto_reassign_recent[key] = time.time()

    def _prune_auto_reassign_cache(self, now: float | None = None) -> None:
        """Public variant (kept for backwards compatibility in tests)."""
        with self._auto_reassign_lock:
            self._prune_auto_reassign_cache_locked(now)

    def _prune_auto_reassign_cache_locked(self, now: float | None = None) -> None:
        now_ts = time.time() if now is None else float(now)
        expiry = max(float(self._auto_reassign_cooldown_s) * 4.0, 5.0)
        stale = [key for key, ts in self._auto_reassign_recent.items() if (now_ts - float(ts or 0.0)) >= expiry]
        for key in stale:
            self._auto_reassign_recent.pop(key, None)

    def schedule_reconnect(self, reason: str) -> None:
        now = time.time()
        if self.controller._reconnect_in_progress or (now - self.controller._last_reconnect_ts) < 5:
            return
        self.controller._last_reconnect_ts = now
        self.controller._reconnect_in_progress = True
        # Mark that the gateway link was lost during active use; if the next
        # ``discoverPort`` cannot find a matching device (e.g. user pulled the
        # USB cable), ``_record_gateway_error`` will upgrade the resulting
        # NOT_FOUND to LINK_LOST so the backoff timer keeps polling.
        self.controller._link_recovery_pending = True

        def _reconnect():
            try:
                logger.warning("RaceLink: attempting gateway transport reconnect after error: %s", reason)
                # Stage 2 Part 5: close every attached transport, not
                # just the primary slot. At N>1 the secondary transports
                # would otherwise hold their exclusive OS file-descriptor
                # locks across the reconnect and the upcoming
                # ``enumerate_all`` probe would see every port as
                # ``PORT_BUSY``. ``_close_all_transports`` is idempotent
                # and is the same helper ``discoverPort`` uses on entry,
                # so the cleanup is guaranteed even when ``discoverPort``
                # bails before its own close step.
                try:
                    closer = getattr(self.controller, "_close_all_transports", None)
                    if callable(closer):
                        closer()
                    else:
                        # Older fakes / tests may not have the helper —
                        # fall back to the legacy single-slot close.
                        if self.transport:
                            self.transport.close()
                        self.controller.transport = None
                except Exception:
                    logger.debug("RaceLink: error closing transports during reconnect", exc_info=True)
                # Transport-level disconnect is automatic by definition -- mark
                # the reconnect attempt accordingly so it does not escalate to
                # ERROR on the RotorHazard log bridge.
                self.controller.discoverPort({}, origin="auto")
            finally:
                self.controller._reconnect_in_progress = False

        # A8: named so threading.enumerate() / py-spy outputs are
        # legible during a reconnect storm.
        threading.Thread(target=_reconnect, daemon=True, name="rl-reconnect").start()

    def pending_try_match(self, ev: dict) -> None:
        # A5: snapshot via the controller helper, then use compare-and-
        # clear semantics so a freshly-stamped expectation from the TX
        # thread cannot be silently wiped by our clear below.
        # Stage 2 Part 3: scope the lookup to the event's source gateway
        # so transport-B's window-closed cannot wipe transport-A's slot.
        p = self.controller.read_pending_expect(gateway_id=ev.get("gateway_id"))
        if not p:
            return

        try:
            sender3_hex = self.controller._to_hex_str(ev.get("sender3")).upper()
            if not sender3_hex:
                return
            if sender3_hex != (p.get("sender_last3") or "").upper():
                return

            opcode7 = int(p.get("opcode7", -1)) & 0x7F
            policy = int(response_policy(opcode7))

            matched = False
            if policy == int(protocol_rules.RESP_ACK):
                if int(ev.get("opc", -1)) == int(LP.OPC_ACK) and int(ev.get("ack_of", -2)) == opcode7:
                    matched = True
            elif policy == int(protocol_rules.RESP_SPECIFIC):
                rsp_opc = int(response_opcode(opcode7))
                if int(ev.get("opc", -1)) == rsp_opc:
                    matched = True

            if matched:
                dev = p.get("dev")
                with self._state_lock():
                    if dev:
                        dev.mark_online()
                # CAS-clear: only drops the expectation if it's still the
                # one we matched on. If the TX thread has stamped a new
                # one mid-flight, leave it alone.
                self.controller.clear_pending_expect_if(p)
        except Exception:
            logger.exception("RaceLink: pending match failed")

    def pending_window_closed(self, ev: dict) -> None:
        # A5: snapshot + CAS-clear, same shape as pending_try_match. A
        # window-closed without a reply means *the expectation we were
        # tracking* timed out — if the TX thread has since stamped a
        # new one, that new request is for a different operation and
        # must not be wiped.
        # Stage 2 Part 3: only the source-gateway's slot can be cleared
        # by its own window-closed event.
        p = self.controller.read_pending_expect(gateway_id=ev.get("gateway_id"))
        if not p:
            return

        try:
            dev = p.get("dev")
            rule = p.get("rule")
            opcode7 = int(p.get("opcode7", -1)) & 0x7F
            name = getattr(rule, "name", f"opc=0x{opcode7:02X}")
            with self._state_lock():
                if dev:
                    dev.mark_offline(f"Missing reply ({name})")
        finally:
            self.controller.clear_pending_expect_if(p)
