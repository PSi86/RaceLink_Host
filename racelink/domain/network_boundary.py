"""Network-boundary enforcement helpers (Stage 3 Part B).

A RaceLink network is the unit of LoRa airtime, RF settings, and
operator-visible identity. A group spans *devices on the same
network*; a device joining a group on a different network would
either receive frames meant for someone else's airspace, or — more
commonly — never receive any frame because the gateway driving that
group's airspace sits on a different frequency.

The validators here detect that boundary violation before the
operator commits to a state that would silently break. They are
pure functions (no side effects) so the same code can run:

  * Server-side, with HTTP 400 on the CRUD endpoints
    (``/api/devices/update-meta``, group-create, scene runner).
  * Client-side as a future "this would be rejected" preview hint
    in the Vue UI (Stage 4).

Out of scope for Stage 3:

  * Re-grouping the device's network as a side effect of moving it
    into another network's group. That is a *migration* (which
    rewrites the device's NVS-stored RF settings), handled by the
    Stage-3 :mod:`racelink.services.rf_migration_service`. The
    boundary validator is the "you can't do this without a
    migration" gate.
  * The "Unconfigured" pseudo-group (id 0). Membership in id 0
    means "no group"; the validator treats it as
    network-agnostic — moving a device into Unconfigured is
    always allowed regardless of its network.
"""

from __future__ import annotations

from typing import Iterable, Optional


# Group id 0 is the system-defined "Unconfigured" pseudo-group.
# Devices land here when the operator removes them from a group;
# the host never sends ``OPC_SET_GROUP`` for it. Multi-network
# enforcement treats it as a network-agnostic sink — moving any
# device into Unconfigured is always allowed.
_UNCONFIGURED_GROUP_ID = 0


def _device_network_id(dev) -> Optional[str]:
    """Pull ``network_id`` off an ``RL_Device``-shaped object.

    Returns ``None`` when the device has no binding yet (e.g. a
    freshly-discovered device pre-network-assignment, or a v1
    payload whose persistence-migration step has not yet run).
    """
    nid = getattr(dev, "network_id", None)
    if nid is None and isinstance(dev, dict):
        nid = dev.get("network_id")
    return str(nid) if nid else None


def _group_network_id(group) -> Optional[str]:
    nid = getattr(group, "network_id", None)
    if nid is None and isinstance(group, dict):
        nid = group.get("network_id")
    return str(nid) if nid else None


def _device_addr(dev) -> str:
    addr = getattr(dev, "addr", None)
    if addr is None and isinstance(dev, dict):
        addr = dev.get("addr")
    return str(addr or "")


def _group_name(group) -> str:
    name = getattr(group, "name", None)
    if name is None and isinstance(group, dict):
        name = group.get("name")
    return str(name or "")


class NetworkBoundaryViolation(ValueError):
    """Raised when a network-spanning operation would be unsafe.

    Carries the operator-readable ``reason`` and a structured
    ``detail`` dict so the HTTP layer can surface both a toast
    string and a JSON shape the WebUI can consume.
    """

    def __init__(self, reason: str, detail: dict):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def validate_group_membership(
    devices: Iterable,
    target_group,
    *,
    target_group_id: Optional[int] = None,
) -> None:
    """Refuse a bulk regroup that would span network boundaries.

    Raises :class:`NetworkBoundaryViolation` on the first detected
    conflict; returns ``None`` on success.

    Two conflict shapes:

      * **devices_span_multiple_networks** — the operator selected
        devices that don't all share a network. Moving them into the
        same group is meaningless regardless of the target group.
      * **group_network_mismatch** — every device agrees on a
        network, but the target group is on a different one.

    ``target_group_id == 0`` (Unconfigured) short-circuits the check
    — moving a device into Unconfigured is the "remove from group"
    action and is always allowed. Pass the literal id alongside the
    group object because the group records carry positional
    semantics in the current model (``id`` is its index in the
    repository's list).

    Devices without a ``network_id`` (legacy / freshly-discovered)
    are ignored for the multi-network-spread check — a single
    unbound device cannot conflict with anything. They *can* still
    conflict with the target group's network: an explicit group
    membership on a bound group requires the device to share that
    network.
    """
    # Unconfigured is the "remove from group" sink — never a conflict.
    if target_group_id is not None and int(target_group_id) == _UNCONFIGURED_GROUP_ID:
        return

    dev_list = list(devices)
    networks_seen: dict[str, list[str]] = {}
    for dev in dev_list:
        nid = _device_network_id(dev)
        if nid is None:
            continue
        networks_seen.setdefault(nid, []).append(_device_addr(dev))

    # Subcase 1: devices themselves disagree on which network they're on.
    if len(networks_seen) > 1:
        raise NetworkBoundaryViolation(
            reason=(
                "Selected devices span multiple networks "
                f"({len(networks_seen)} different network_ids); "
                "move them one network at a time."
            ),
            detail={
                "code": "devices_span_multiple_networks",
                "networks": {
                    nid: list(macs) for nid, macs in networks_seen.items()
                },
            },
        )

    # Subcase 2: devices agree on a network, but target group is elsewhere.
    target_nid = _group_network_id(target_group)
    if not networks_seen or target_nid is None:
        # Either no bound devices (everything's legacy / fresh) or the
        # target group has no network constraint — nothing to enforce.
        return
    device_nid = next(iter(networks_seen.keys()))
    if device_nid != target_nid:
        raise NetworkBoundaryViolation(
            reason=(
                f"Group '{_group_name(target_group) or target_group_id}' "
                f"belongs to a different network than the selected devices. "
                "Migrate the devices to the target network first "
                "(Stage-3 RF migration), then re-run this action."
            ),
            detail={
                "code": "group_network_mismatch",
                "device_network_id": device_nid,
                "group_network_id": target_nid,
                "device_macs": list(networks_seen[device_nid]),
                "group_id": target_group_id,
                "group_name": _group_name(target_group),
            },
        )
