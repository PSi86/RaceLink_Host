"""Discovery service for device identification via the gateway.

Coordinates an OPC_DEVICES broadcast, collects ``IDENTIFY_REPLY``
events from any node that responds within the gateway's RX
window, and reconciles the replies into the device repository
(creating new ``RL_Device`` records, updating existing ones,
preserving operator-set name/groupId for already-known macs).

Public API:

* :meth:`DiscoveryService.discover_devices` — fire one OPC_DEVICES
  on a single ``group_filter`` value, drain the reply window,
  return ``{"found": N, ...}``. The default ``group_filter=255``
  is the historical wire fallback; the operator-facing default is
  ``group_filter=0`` (Unconfigured) and is set by the API caller —
  see ``docs/reference/broadcast-ruleset.md`` for the design rule.
* :meth:`DiscoveryService.discover_devices_in_groups` — fan-out
  helper that loops :meth:`discover_devices` once per group id and
  merges responders. Used by the Web UI's "Discover in: All
  groups" option to reach a fleet whose devices have been
  re-flashed / moved between gateways and may sit in any of the
  known groups. The future
  [group-agnostic re-identification](../../docs/roadmap.md)
  feature would replace the loop with a single packet.

Threading: typically driven by the task manager from a worker
thread (the operator clicks "Discover" → web route → task →
this service). The reply-collection path builds a wildcard
:class:`PendingMatcher` and waits via
:meth:`GatewayService.send_and_match`; the matcher exits on
idle-timeout after the last late-comer goes quiet.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, List, Optional

from ..transport import LP, mac_last3_from_hex
from ..transport.broadcast_fanout import broadcast_fanout, resolve_broadcast_transports
from . import rf_timing
from .pending_requests import PendingMatcher

logger = logging.getLogger(__name__)


class DiscoveryService:
    def __init__(self, controller, gateway_service):
        self.controller = controller
        self.gateway_service = gateway_service

    @property
    def transport(self):
        return getattr(self.controller, "transport", None)

    def discover_devices(self, *, group_filter=255, target_device=None,
                         add_to_group=-1, transport=None) -> dict:
        """Trigger an ``OPC_DEVICES`` broadcast and collect IDENTIFY_REPLYs.

        Transport selection:

        * ``transport`` set (Stage 3 Part F) — scan exactly that gateway
          instance. The channel-scan service uses this to walk a region's
          channels on one gateway while the others stay on their settings.
        * ``target_device`` set — unicast re-identify of one device; routed
          to the device's bound transport.
        * Neither set (the operator's "Discover Devices") — **fan out across
          every attached transport**. A broadcast discovery must reach every
          RF gateway *and* every Ethernet network; the "primary" transport
          slot must never gate which gateways are probed (otherwise the set
          that gets served depends on attach order). This mirrors the
          status-service broadcast fan-out (Bug 5). Replies route into
          per-``gateway_id`` registry buckets, so concurrent RX from
          multiple gateways cannot cross-talk.
        """
        # Resolve the transports to probe (see docstring).
        if transport is not None:
            transports = [transport]
        elif target_device is not None:
            routed = None
            try:
                routed = self.controller.transport_for_device(target_device.addr)
            except Exception:
                logger.debug(
                    "discover_devices: transport_for_device raised for %s",
                    getattr(target_device, "addr", "?"), exc_info=True,
                )
            transports = [t for t in (routed or self.transport,) if t is not None]
        else:
            # Operator broadcast: every attached transport, never just the
            # primary slot. ``resolve_broadcast_transports(target=None)``
            # returns all attached (with the singleton fallback for the
            # early-boot / reconnect window); a single-gateway deployment
            # collapses to a 1-element list and behaves like the legacy
            # single-send path.
            transports = resolve_broadcast_transports(
                self.controller, target=None,
                label="discover_devices",
                fallback_transport=self.transport,
            )

        if not transports:
            logger.warning("getDevices: communicator not ready")
            return {"found": 0, "responders": set(), "assigned_group": None}

        # Probe each transport. A 1-element list runs inline (byte-identical
        # to the pre-fan-out path); 2+ transports fan out in parallel daemon
        # threads so wall-clock is ~1 × (airtime + reply window) instead of
        # N ×. The outer join budget sits just above the per-transport
        # matcher ceiling so the inner idle/max-timeout owns the wait.
        responders: set[str] = set()
        found = 0
        if len(transports) == 1:
            sub = self._discover_on_transport(transports[0], group_filter, target_device)
            responders |= set(sub.get("responders") or ())
            found += int(sub.get("found") or 0)
        else:
            fanout_results = broadcast_fanout(
                transports,
                lambda t: self._discover_on_transport(t, group_filter, target_device),
                timeout_s=rf_timing.COLLECT_MAX_CEILING_S + 1.0,
                label="discover_devices",
            )
            for r in fanout_results:
                sub = r.outcome if isinstance(r.outcome, dict) else None
                if sub is None:
                    # Worker raised / timed out — broadcast_fanout already
                    # logged the per-transport detail; skip its (absent)
                    # responders rather than poisoning the merged result.
                    continue
                responders |= set(sub.get("responders") or ())
                found += int(sub.get("found") or 0)

        assigned_group = None
        if add_to_group > 0 and add_to_group < 255:
            assigned_group = int(add_to_group)
            for addr in responders:
                dev = self.controller.getDeviceFromAddress(addr)
                if not dev:
                    continue
                dev.groupId = assigned_group
                self.controller.setNodeGroupId(dev)
            # First device(s) landing in a network-agnostic group stamp its
            # network (and thus RF/Ethernet kind); see
            # ``Controller.reconcile_group_network``.
            try:
                self.controller.reconcile_group_network(assigned_group)
            except Exception:
                logger.debug(
                    "discover_devices: reconcile_group_network raised for group %s",
                    assigned_group, exc_info=True,
                )

        return {"found": found, "responders": responders, "assigned_group": assigned_group}

    def _discover_on_transport(self, transport, group_filter, target_device) -> dict:
        """Send one ``OPC_DEVICES`` on a single transport and collect the
        IDENTIFY_REPLYs that arrive through it.

        Returns ``{"found": int, "responders": set[str]}``. The matcher is
        scoped to the transport's ``ident_mac`` so, under the multi-transport
        fan-out, each transport only counts the replies that came back on its
        own radio (the per-``gateway_id`` registry bucket).
        """
        self.gateway_service.install_transport_hooks(transport=transport)

        if target_device is None:
            recv3 = b"\xFF\xFF\xFF"
            group_id = int(group_filter) & 0xFF
        else:
            recv3 = mac_last3_from_hex(target_device.addr)
            group_id = int(target_device.groupId) & 0xFF

        logger.debug(
            "GET_DEVICES -> recv3=%s group=%d flags=%d gw=%s",
            recv3.hex().upper(), group_id, 0, getattr(transport, "ident_mac", "?"),
        )

        try:
            transport.drain_events(0.0)
        except Exception:
            logger.debug("RaceLink: drain_events before discover raised", exc_info=True)

        # Plan Phase C (revised): GET_DEVICES is the one call where the
        # responder count is genuinely unknown (a fresh device could answer),
        # so we keep the hard ceiling at 5 s. Idle-based termination still
        # lets us return early once the last late-comer has gone quiet for
        # 600 ms. ``expected_count`` is a large sentinel so the matcher
        # never exits on count — only idle or max-timeout terminate it.
        matcher = PendingMatcher(
            sender_filter=None,  # wildcard — any device may answer
            expected_opcode=int(LP.OPC_DEVICES) & 0x7F,
            gateway_id=getattr(transport, "ident_mac", None),
            discriminator_field="reply",
            discriminator_value="IDENTIFY_REPLY",
            expected_count=2**31,
            idle_timeout_s=rf_timing.COLLECT_IDLE_TIMEOUT_S,
            max_timeout_s=rf_timing.COLLECT_MAX_CEILING_S,
        )
        replies, _reason = self.gateway_service.send_and_match(
            lambda: transport.send_get_devices(recv3=recv3, group_id=group_id, flags=0),
            matcher,
            transport=transport,
        )

        responders: set[str] = set()
        for ev in replies:
            mac6 = ev.get("mac6")
            if isinstance(mac6, (bytes, bytearray)) and len(mac6) == 6:
                responders.add(bytes(mac6).hex().upper())
                continue
            sender_hex = self.controller._to_hex_str(ev.get("sender3"))
            if sender_hex:
                responders.add(sender_hex.upper())
        return {"found": len(replies), "responders": responders}

    def discover_devices_in_groups(
        self,
        *,
        group_ids: Iterable[int],
        add_to_group: int = -1,
    ) -> dict:
        """Sweep discovery across multiple group filters.

        Calls :meth:`discover_devices` once per id in ``group_ids``,
        merges the responder sets, and returns the aggregated result.
        Sequential (one OPC_DEVICES + RX window per id) — matches the
        operator-initiated cadence of the discovery dialog. The future
        [group-agnostic re-identification](../../docs/roadmap.md)
        feature replaces this with a single packet.

        Returns the same shape as :meth:`discover_devices`: ``{"found":
        <total replies across all groups>, "responders": <merged
        set>, "assigned_group": <add_to_group if applied else None>}``.
        """
        merged_responders: set = set()
        total_found = 0
        last_assigned: Optional[int] = None
        for gid in group_ids:
            try:
                gid_int = int(gid)
            except (TypeError, ValueError):
                continue
            if not 0 <= gid_int <= 254:
                continue
            result = self.discover_devices(
                group_filter=gid_int,
                add_to_group=add_to_group,
            )
            total_found += int(result.get("found", 0) or 0)
            responders = result.get("responders") or set()
            merged_responders.update(responders)
            assigned = result.get("assigned_group")
            if assigned is not None:
                last_assigned = assigned
        return {
            "found": total_found,
            "responders": merged_responders,
            "assigned_group": last_assigned,
        }
