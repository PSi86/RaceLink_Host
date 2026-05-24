"""Explicit broadcast scope for multi-network deployments.

Background
----------
A broadcast send (``recv3 == FFFFFF``) addresses every device in a
LoRa network. In a multi-gateway deployment the host can hold several
transports concurrently, each driving its own network. Until Stage 3
Part G the broadcast helpers either fanned out across ALL attached
transports (``GatewayService.send_sync``) or routed via the group's
network and hit just one (``ControlService.send_group_preset`` and
siblings) — both inconsistent with each other and with caller intent.

The :class:`BroadcastTarget` is the explicit answer: every broadcast
call carries the set of networks it addresses, resolved to network
ids that the controller already maps to transports via
``transport_for_network``.

Design rules
------------
* Construction is explicit. There is no implicit "current network"
  fallback — UI focus and scene targeting are independent and the
  service layer cannot tell them apart.
* Factory constructors cover the recurring intents:
    - :meth:`from_ids` — caller-supplied set (the common case;
      scene runner builds one from the scene's action targets).
    - :meth:`single` — one network (group/device-bound sends
      can construct this via ``transport_for_network`` lookups).
    - :meth:`all_attached` — health probes / fleet-wide ops that
      genuinely want to reach every gateway the host knows about.
* Immutable + hashable so it can flow through threaded code paths
  without locking concerns.

Threading + ordering
--------------------
``network_ids`` is an ordered tuple. Order is irrelevant for
correctness but determines the USB-write order in fan-out callers —
the first id's transport gets its frame onto the wire ~1 ms before
the second, etc. Across N gateways the total skew is N × USB-write
latency (sub-ms each), well under LoRa airtime, so the air times
still overlap closely enough for arm_on_sync coordination to work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple


@dataclass(frozen=True)
class BroadcastTarget:
    """Explicit set of ``network_id`` values a broadcast addresses.

    Use the classmethods to construct — the bare constructor is
    available for tests but caller code should go through
    :meth:`from_ids` / :meth:`single` / :meth:`all_attached` so the
    intent is visible at the call site.
    """

    network_ids: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.network_ids:
            raise ValueError("BroadcastTarget needs at least one network_id")
        # Defensive coerce: dataclass(frozen=True) bypasses __setattr__,
        # so use object.__setattr__ to normalise to tuple-of-str.
        normalised = tuple(str(nid) for nid in self.network_ids)
        if normalised != self.network_ids:
            object.__setattr__(self, "network_ids", normalised)

    # ---- Factories ------------------------------------------------------

    @classmethod
    def from_ids(cls, network_ids: Iterable[str]) -> "BroadcastTarget":
        """Build a target from an explicit set of network ids.

        Duplicates are preserved in input order (the first occurrence
        wins); empty input raises ``ValueError`` to surface a caller bug
        instead of silently turning into a no-op send.
        """
        seen: set = set()
        ordered: list = []
        for nid in network_ids or ():
            s = str(nid) if nid is not None else ""
            if not s or s in seen:
                continue
            seen.add(s)
            ordered.append(s)
        if not ordered:
            raise ValueError("BroadcastTarget.from_ids: no usable network ids")
        return cls(network_ids=tuple(ordered))

    @classmethod
    def single(cls, network_id: str) -> "BroadcastTarget":
        """One-network target. Equivalent to ``from_ids([network_id])``
        but reads clearer at the call site."""
        if not network_id:
            raise ValueError("BroadcastTarget.single: network_id required")
        return cls(network_ids=(str(network_id),))

    @classmethod
    def all_attached(cls, controller) -> "BroadcastTarget":
        """Every attached transport's bound network. Used by health
        probes / discovery / fleet-wide notify where the caller really
        does want every gateway the host currently knows about.

        Resolves via each transport's stamped ``network_id`` (set by
        ``Controller._bind_transport_to_network`` during attach). A
        transport without a bound network is skipped — it would have
        no addressable scope on the wire anyway.

        Raises ``ValueError`` when there are no attached + bound
        transports — preferable to silently turning the broadcast into
        a no-op.
        """
        ids: list = []
        seen: set = set()
        for t in (getattr(controller, "transports", None) or ()):
            nid = str(getattr(t, "network_id", "") or "")
            if not nid or nid in seen:
                continue
            seen.add(nid)
            ids.append(nid)
        if not ids:
            raise ValueError(
                "BroadcastTarget.all_attached: no transport has a bound network_id"
            )
        return cls(network_ids=tuple(ids))

    # ---- Resolution -----------------------------------------------------

    def resolve_transports(self, controller) -> list:
        """Map ``network_ids`` to live transport instances via
        ``controller.transport_for_network``. Networks without an
        attached transport are skipped (with no error) — the host's
        intent is "send to these networks, best effort". A fully empty
        result is also returned without raising so the caller can log
        + decide what to do.
        """
        out: list = []
        seen: set = set()
        for nid in self.network_ids:
            t = controller.transport_for_network(nid)
            if t is None:
                continue
            ident = id(t)
            if ident in seen:
                # Two ids mapping to the same transport (unusual but
                # legal during reconnect/swap windows) — coalesce so a
                # single transport is not sent the same broadcast twice.
                continue
            seen.add(ident)
            out.append(t)
        return out
