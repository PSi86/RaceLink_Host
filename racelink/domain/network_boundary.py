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

from typing import Any, Iterable, List, Mapping, Optional


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


def _network_kind(net) -> str:
    """Pull the ``kind`` discriminator off an ``RL_Network``-shaped object.

    Defaults to ``"rf"`` for ``None`` / missing / empty so legacy networks
    (pre-``kind``) and any forward-incompatible value compare as the
    historical RF kind — matching ``RL_Network._normalize_kind``.
    """
    kind = getattr(net, "kind", None)
    if kind is None and isinstance(net, Mapping):
        kind = net.get("kind")
    return str(kind).strip().lower() if kind else "rf"


def _network_id_of(net) -> Optional[str]:
    nid = getattr(net, "id", None)
    if nid is None and isinstance(net, Mapping):
        nid = net.get("id")
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


class SceneScopeViolation(ValueError):
    """Raised when a scene's explicit ``network_scope`` is inconsistent
    with its actions — either the scope references unknown networks,
    or an action targets a network outside the scope.

    Sibling to :class:`NetworkBoundaryViolation` (sharing the same
    operator-error shape) but kept distinct so the HTTP layer can
    route scope-related diagnostics to the scene editor without
    confusing them with bulk-regroup boundary checks.
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


def validate_network_kind_match(target_network, source_networks) -> None:
    """Refuse a migration that would move a group/device across network *kinds*.

    RaceLink networks come in kinds (``"rf"``, ``"ethernet"``, …). Migrating
    a group or device from an RF network onto an Ethernet network (or vice
    versa) is physically meaningless — a device speaks exactly one transport,
    not both — so the move is rejected up front rather than silently
    producing an unreachable membership or letting an RF re-flash run against
    a node that has no radio.

    ``target_network`` is the destination ``RL_Network`` (or dict).
    ``source_networks`` is an iterable of the current ``RL_Network`` objects
    (or dicts) the migrated groups/devices live on; ``None`` entries
    (unbound / legacy) are ignored.

    Raises :class:`NetworkBoundaryViolation` (code ``network_kind_mismatch``)
    listing every source network whose kind differs from the target;
    returns ``None`` on success.
    """
    target_kind = _network_kind(target_network)
    mismatched: list[dict] = []
    seen: set = set()
    for net in source_networks:
        if net is None:
            continue
        src_kind = _network_kind(net)
        if src_kind == target_kind:
            continue
        nid = _network_id_of(net)
        key = (nid, src_kind)
        if key in seen:
            continue
        seen.add(key)
        mismatched.append({"network_id": nid, "kind": src_kind})

    if mismatched:
        raise NetworkBoundaryViolation(
            reason=(
                "Cannot migrate across network types: the target network is "
                f"'{target_kind}' but the selection lives on "
                f"{len(mismatched)} '{mismatched[0]['kind']}'"
                f"{'/…' if len({m['kind'] for m in mismatched}) > 1 else ''} "
                "network(s). RF and Ethernet networks cannot be mixed."
            ),
            detail={
                "code": "network_kind_mismatch",
                "target_kind": target_kind,
                "target_network_id": _network_id_of(target_network),
                "source_networks": mismatched,
            },
        )


# ---------------------------------------------------------------------------
# Per-scene network_scope consistency
# ---------------------------------------------------------------------------
#
# Sibling to :func:`validate_group_membership`. The structural shape of
# ``scene["network_scope"]`` is validated in
# :func:`racelink.services.scenes_service._canonical_network_scope`
# (repository-free); the cross-action subset check lives here because it
# needs the controller's device + group repositories. The web layer
# invokes this validator after the scenes_service has accepted the
# canonical shape, so any failure is operator-visible as HTTP 400.

def validate_scene_scope_consistency(
    scene: Mapping[str, Any],
    *,
    controller: Any,
) -> None:
    """Refuse a scene whose explicit ``network_scope`` is internally
    inconsistent.

    Two conflict shapes (both :class:`SceneScopeViolation`):

      * **unknown_network_id** — the scope references a network that
        is not in the controller's network_repository. Save would
        succeed but the next operator who edits this scene would see
        a degraded "stale id" indicator.
      * **scope_violation** — an action's resolved target lies on a
        network not in the explicit scope. The save would otherwise
        produce a scene whose runtime behaviour silently drops the
        offending action (no transport routes to a non-scope group/
        device).

    Returns ``None`` (auto-mode scenes always pass) on success.

    ``scene`` is the canonicalised scene dict (post-``create()`` /
    ``update()`` shape). ``controller`` must expose
    ``network_repository``, ``device_repository``, and
    ``group_repository`` — production paths always satisfy this; tests
    can stub.
    """
    scope = scene.get("network_scope")
    if not isinstance(scope, Mapping) or scope.get("mode") != "explicit":
        return  # auto mode (or missing) — nothing to enforce here

    explicit_ids = [str(n) for n in (scope.get("network_ids") or ())]
    if not explicit_ids:
        # Should be unreachable post-canonicalization (the canonicalizer
        # rejects empty explicit lists), but the validator is defensive.
        raise SceneScopeViolation(
            reason="Explicit network scope is empty.",
            detail={"code": "scope_empty"},
        )

    # Check 1: every id in the explicit scope must currently exist in
    # the network repository. Unknown ids → hard reject. The reasoning
    # is documented in the plan: the editor has full repo visibility,
    # so operators should never reach Save with a stale id. If they do,
    # surface clearly. (Runtime degradation only kicks in for files
    # already on disk via :func:`scene_network_ids`'s soft-filter.)
    net_repo = getattr(controller, "network_repository", None)
    if net_repo is not None:
        known: set = set()
        try:
            for n in net_repo.list() or ():
                nid = getattr(n, "id", None)
                if nid:
                    known.add(str(nid))
        except Exception:
            # swallow-ok: a malformed repo must not crash save; treat
            # as "everything unknown" → all ids reject below.
            known = set()
        unknown = [nid for nid in explicit_ids if nid not in known]
        if unknown:
            raise SceneScopeViolation(
                reason=(
                    f"Scope references {len(unknown)} unknown network "
                    f"id(s): {', '.join(unknown)}."
                ),
                detail={
                    "code": "unknown_network_id",
                    "unknown_ids": unknown,
                    "scope": list(explicit_ids),
                },
            )

    # Check 2: every action's resolved network membership must be a
    # subset of the explicit scope. Lazy import to avoid a circular
    # dependency: scene_network_scope imports from this module's
    # sibling services, and the import order during package init
    # could fail if we eagerly imported it at module load.
    from racelink.services.scene_network_scope import (
        _network_ids_for_target,
    )
    scope_set = set(explicit_ids)
    actions = scene.get("actions") or ()

    def _check_action(action: Mapping[str, Any], index: int) -> None:
        if not isinstance(action, Mapping):
            return
        target = action.get("target")
        if isinstance(target, Mapping):
            kind = target.get("kind")
            # Broadcast target (group 255) is always in-scope by
            # construction: at runtime the scene's explicit scope is
            # the fan-out target. No subset check needed.
            if kind != "broadcast":
                resolved = list(_network_ids_for_target(controller, target))
                if resolved:
                    offending = [nid for nid in resolved if nid not in scope_set]
                    if offending:
                        raise SceneScopeViolation(
                            reason=(
                                f"Action #{index} targets network(s) "
                                f"outside the scene's scope: "
                                f"{', '.join(offending)}."
                            ),
                            detail={
                                "code": "scope_violation",
                                "offending_action_index": index,
                                "action_network_ids": resolved,
                                "scope": list(explicit_ids),
                            },
                        )
        # Recurse into offset_group / future container children.
        for child_index, child in enumerate(action.get("actions") or ()):
            _check_action(child, index)  # reuse parent index so the
            # operator sees one row to fix even on nested violations
            # (the child position is opaque in the action list).
            del child_index  # silence the unused-var hint

    for idx, action in enumerate(actions):
        _check_action(action, idx)
