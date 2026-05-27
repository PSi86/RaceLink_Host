"""Status polling service for current device state via the gateway.

Sends OPC_STATUS (broadcast or to a specific group) and updates
each device's ``last_seen_ts``, RSSI/SNR, online flag, and
voltage from the resulting ``STATUS_REPLY`` events. Devices
that don't respond within the RX window are marked offline
("Missing reply (STATUS)").

Public API:

* ``get_status(group_filter=255, target_device=None) -> dict`` —
  fires the broadcast (or unicast for a single device), collects
  replies, returns ``{"updated": N, "responders": set, ...}``.

Multi-gateway (Bug 5 fix, 2026-05-26): a broadcast
``get_status(target_device=None)`` fans out across EVERY attached
transport — one OPC_STATUS broadcast per gateway, per-transport
snapshot + per-transport mark_offline. Without this, a 2-gateway
setup would see one device on the off-radio gateway fail the
broadcast and need a per-device unicast retry every time.

Fan-out timing (2026-05-26 follow-up): the per-transport sends +
reply-collects run in parallel via ``broadcast_fanout`` — one
daemon thread per transport, each with its own gateway_id-tagged
PendingMatcher. Wall-clock collapses from N × (airtime + reply-
window) to ~1 × (airtime + reply-window). Replies route into
independent registry buckets keyed by ``gateway_id`` so concurrent
RX from two gateways cannot cross-talk.

Threading: same shape as :class:`DiscoveryService`. Typically
runs in a task-manager worker thread (operator clicks
"Get Status").
"""

from __future__ import annotations

import logging

from ..transport import LP, mac_last3_from_hex
from ..transport.broadcast_fanout import broadcast_fanout, resolve_broadcast_transports
from . import rf_timing
from .pending_requests import PendingMatcher

logger = logging.getLogger(__name__)


class StatusService:
    def __init__(self, controller, gateway_service):
        self.controller = controller
        self.gateway_service = gateway_service

    @property
    def transport(self):
        return getattr(self.controller, "transport", None)

    # ---- public API -------------------------------------------------

    def get_status(self, *, group_filter=255, target_device=None) -> dict:
        """Poll device status. ``target_device`` selects a single-mac
        unicast path (routed via its bound transport); otherwise a
        broadcast OPC_STATUS fans out across every attached transport.
        Returns ``{"updated", "responders", "got_closed", "retried",
        "retried_responders"}``."""
        self.gateway_service.install_transport_hooks()

        if target_device is not None:
            return self._get_status_unicast(target_device)

        # Broadcast: every attached transport gets its own OPC_STATUS.
        # ``resolve_broadcast_transports(target=None)`` returns every
        # live transport (with the singleton fallback for early-boot
        # reconnect windows). A single-gateway deployment lands on a
        # 1-element list and behaves like the pre-fan-out path.
        transports = resolve_broadcast_transports(
            self.controller, target=None,
            label="get_status_broadcast",
            fallback_transport=self.transport,
        )
        if not transports:
            logger.warning("getStatus: communicator not ready")
            return {
                "updated": 0,
                "responders": set(),
                "got_closed": False,
                "retried": 0,
                "retried_responders": set(),
            }

        # Threaded fan-out: each transport gets its own thread doing
        # the full per-transport send + reply-collect + per-transport
        # mark_offline. Reply matchers are gateway_id-tagged so
        # concurrent RX from two gateways routes into independent
        # registry buckets (see RX-concurrency audit in this commit's
        # plan file). Wall-clock ≈ 1 × airtime instead of N × airtime.
        #
        # Fan-out timeout > COLLECT_MAX_CEILING_S so the outer join
        # never races the inner send_and_match timeout — the inner
        # ceiling owns the wait, the outer just caps the worst-case
        # "thread stuck" scenario.
        fanout_results = broadcast_fanout(
            transports,
            lambda t: self._get_status_broadcast_on_transport(t, group_filter),
            timeout_s=rf_timing.COLLECT_MAX_CEILING_S + 1.0,
            label="get_status_broadcast",
        )

        aggregated_updated = 0
        aggregated_responders: set[str] = set()
        aggregated_retried = 0
        aggregated_retried_responders: set[str] = set()
        for r in fanout_results:
            sub = r.outcome if isinstance(r.outcome, dict) else None
            if sub is None:
                # Worker raised or timed out — broadcast_fanout already
                # logged the per-transport details. Skip aggregation
                # so a single transport's failure can't poison the
                # others' counts; mark_offline for that gateway's
                # devices falls to the next get_status call.
                continue
            aggregated_updated += int(sub.get("updated") or 0)
            aggregated_responders |= set(sub.get("responders") or ())
            aggregated_retried += int(sub.get("retried") or 0)
            aggregated_retried_responders |= set(sub.get("retried_responders") or ())

        return {
            "updated": aggregated_updated,
            "responders": aggregated_responders,
            "got_closed": True,
            "retried": aggregated_retried,
            "retried_responders": aggregated_retried_responders,
        }

    # ---- per-transport broadcast -----------------------------------

    def _devices_on_transport(self, transport) -> list:
        """Return repo devices whose ``network_id`` resolves to
        ``transport``. Used to scope the broadcast snapshot — a device
        on gateway B must not get marked offline because it didn't
        answer gateway A's broadcast.
        """
        out: list = []
        for dev in self.controller.device_repository.list():
            mac = (getattr(dev, "addr", "") or "")
            if not mac:
                continue
            dev_t = None
            try:
                dev_t = self.controller.transport_for_device(mac)
            except Exception:
                # swallow-ok: routing helper is best-effort. A device
                # without a resolvable transport simply isn't part of
                # this gateway's snapshot — it'll be picked up (or
                # skipped) when its own gateway's iteration runs.
                logger.debug(
                    "_devices_on_transport: transport_for_device raised for %s",
                    mac, exc_info=True,
                )
            if dev_t is transport:
                out.append(dev)
        return out

    def _get_status_broadcast_on_transport(self, transport, group_filter) -> dict:
        """Broadcast OPC_STATUS on a single transport; collect + mark
        offline only for devices bound to THAT transport. The matcher
        is scoped to the transport's ``ident_mac`` so replies leaking
        through from other registries can't bias this transport's
        count."""
        recv3 = b"\xFF\xFF\xFF"
        group_id = int(group_filter) & 0xFF
        transport_ident = getattr(transport, "ident_mac", None)

        # Per-transport snapshot — keeps mark_offline scoped to devices
        # whose network this transport actually owns.
        local_devices = self._devices_on_transport(transport)
        if group_filter == 255:
            snapshot_targets = list(local_devices)
        else:
            snapshot_targets = [
                dev for dev in local_devices
                if int(getattr(dev, "groupId", 0)) == int(group_filter)
            ]

        was_online_before: set[str] = set()
        for dev in snapshot_targets:
            if bool(getattr(dev, "link_online", False)):
                mac = (getattr(dev, "addr", "") or "").upper()
                if mac:
                    was_online_before.add(mac)

        try:
            transport.drain_events(0.0)
        except Exception:
            logger.debug("RaceLink: drain_events before get_status raised", exc_info=True)

        expected_count = len(snapshot_targets)
        max_timeout_s = self.gateway_service.compute_collect_max_timeout(
            expected_count, ceiling_s=rf_timing.COLLECT_MAX_CEILING_S
        )

        matcher = PendingMatcher(
            sender_filter=None,  # wildcard — accept any sender via this transport
            expected_opcode=int(LP.OPC_STATUS) & 0x7F,
            gateway_id=transport_ident,
            discriminator_field="reply",
            discriminator_value="STATUS_REPLY",
            expected_count=expected_count if expected_count > 0 else 2**31,
            idle_timeout_s=rf_timing.COLLECT_IDLE_TIMEOUT_S,
            max_timeout_s=max_timeout_s,
        )
        replies, _reason = self.gateway_service.send_and_match(
            lambda: transport.send_get_status(recv3=recv3, group_id=group_id, flags=0),
            matcher,
            transport=transport,
        )

        responders: set[str] = set()
        for ev in replies:
            mac6 = ev.get("mac6")
            if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                responders.add(bytes(mac6).hex().upper())
                continue
            sender3 = ev.get("sender3")
            if isinstance(sender3, (bytes, bytearray)) and len(sender3) == 3:
                responders.add(bytes(sender3).hex().upper())
        updated = len(replies)

        # Per-device retry for previously-online targets that missed
        # this transport's broadcast — unicast through the device's
        # bound transport (always THIS transport here, since
        # snapshot_targets is already filtered to it).
        retried = 0
        retried_responders: set[str] = set()
        if snapshot_targets:
            missing: list = []
            for dev in snapshot_targets:
                mac = (getattr(dev, "addr", "") or "").upper()
                if not mac:
                    continue
                if mac not in was_online_before:
                    continue
                if mac in responders or mac[-6:] in responders:
                    continue
                missing.append(dev)
            for dev in missing:
                try:
                    sub = self._get_status_unicast(dev)
                except Exception:
                    logger.debug(
                        "RaceLink: per-device status retry failed for %r",
                        getattr(dev, "addr", "?"),
                        exc_info=True,
                    )
                    continue
                retried += 1
                sub_resp = sub.get("responders") if isinstance(sub, dict) else None
                if isinstance(sub_resp, set) and sub_resp:
                    retried_responders |= sub_resp
                    responders |= sub_resp
                    updated += int(sub.get("updated") or 0)

        # mark_offline only for THIS transport's devices that didn't
        # answer (broadcast or retry). Other transports handle their
        # own devices.
        for dev in snapshot_targets:
            try:
                mac = (dev.addr or "").upper()
                if not mac:
                    continue
                if mac not in responders and mac[-6:] not in responders:
                    dev.mark_offline("Missing reply (STATUS)")
            except Exception:
                logger.debug(
                    "mark_offline failed for %r",
                    getattr(dev, "addr", "?"),
                    exc_info=True,
                )

        return {
            "updated": updated,
            "responders": responders,
            "got_closed": True,
            "retried": retried,
            "retried_responders": retried_responders,
        }

    # ---- unicast ----------------------------------------------------

    def _get_status_unicast(self, target_device) -> dict:
        """Unicast OPC_STATUS to a specific device. Routes via the
        device's bound transport (``transport_for_device``) and scopes
        the matcher to that transport's ``ident_mac`` so the reply
        registry can't pick a foreign-network frame."""
        recv3 = mac_last3_from_hex(target_device.addr)
        group_id = int(target_device.groupId) & 0xFF
        unicast_target = bytes(recv3)

        routed_transport = None
        try:
            routed_transport = self.controller.transport_for_device(
                getattr(target_device, "addr", "") or ""
            )
        except Exception:
            # swallow-ok: routing helper is best-effort — fall through
            # to the singleton transport, the gateway_id guard below
            # still gates the send when no ident_mac is reachable.
            logger.debug(
                "get_status: transport_for_device raised for %s",
                getattr(target_device, "addr", "?"), exc_info=True,
            )
        if routed_transport is None:
            routed_transport = self.transport
        status_gateway_id = (
            getattr(routed_transport, "ident_mac", None)
            if routed_transport else None
        )
        if routed_transport is None or not status_gateway_id:
            logger.warning(
                "get_status: target_device %s has no routed transport ident_mac; skipping",
                getattr(target_device, "addr", "?"),
            )
            return {
                "updated": 0,
                "responders": set(),
                "got_closed": False,
                "retried": 0,
                "retried_responders": set(),
            }

        try:
            routed_transport.drain_events(0.0)
        except Exception:
            logger.debug("RaceLink: drain_events before get_status raised", exc_info=True)

        expected_count = 1
        max_timeout_s = self.gateway_service.compute_collect_max_timeout(
            expected_count, ceiling_s=rf_timing.COLLECT_MAX_CEILING_S
        )

        matcher = PendingMatcher(
            sender_filter=frozenset({unicast_target}),
            expected_opcode=int(LP.OPC_STATUS) & 0x7F,
            gateway_id=status_gateway_id,
            discriminator_field="reply",
            discriminator_value="STATUS_REPLY",
            expected_count=expected_count,
            idle_timeout_s=rf_timing.COLLECT_IDLE_TIMEOUT_S,
            max_timeout_s=max_timeout_s,
        )
        replies, _reason = self.gateway_service.send_and_match(
            lambda: routed_transport.send_get_status(
                recv3=recv3, group_id=group_id, flags=0,
            ),
            matcher,
            transport=routed_transport,
        )

        responders: set[str] = set()
        for ev in replies:
            mac6 = ev.get("mac6")
            if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                responders.add(bytes(mac6).hex().upper())
                continue
            sender3 = ev.get("sender3")
            if isinstance(sender3, (bytes, bytearray)) and len(sender3) == 3:
                responders.add(bytes(sender3).hex().upper())
        updated = len(replies)

        if updated == 0:
            try:
                target_device.mark_offline("Missing reply (STATUS)")
            except Exception:
                logger.debug(
                    "mark_offline failed for %r",
                    getattr(target_device, "addr", "?"),
                    exc_info=True,
                )

        return {
            "updated": updated,
            "responders": responders,
            "got_closed": True,
            "retried": 0,
            "retried_responders": set(),
        }
