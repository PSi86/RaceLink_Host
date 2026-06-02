"""Compute the set of LoRa networks a scene's actions touch.

When a scene fires a broadcast OPC_SYNC (the only true cross-network
opcode the runner dispatches today — group-targeted PRESET/CONTROL
actions resolve to a single network via ``transport_for_group``), the
runner uses this helper to decide which networks the SYNC should
land on: the union of every action's resolved network membership.

Why compute on-the-fly instead of persisting on the scene
---------------------------------------------------------
Persisting a ``scene["network_ids"]`` field would drift when a device
or group is moved between networks but the scene isn't re-saved — a
stale persisted value could miss a network the scene now touches
(silent no-op) or include a stale one (cross-network broadcast leaks
into a network the scene no longer cares about). The on-the-fly walk
costs sub-ms for realistic scene sizes (≤ 20 actions, ≤ 200 devices)
and is always consistent with the current repository state. Ad-hoc
"run draft from editor" paths get the same correctness without
having to re-save first.

Target shape contract
---------------------
Each action's ``target`` follows the canonical shape produced by
:func:`scenes_service._canonical_actions`:

    {"kind": "broadcast"}                 # group 255 (all groups)
    {"kind": "groups", "value": [1,2,3]}  # specific group ids
    {"kind": "device", "value": "MAC"}    # specific device mac

Resolution rules
----------------
* "broadcast": every network in the host's network repository. The
  group-255 wire broadcast is by design fleet-wide.
* "groups": each group_id resolves via the group repository to a
  ``network_id``; unknown group ids and groups without
  ``network_id`` are silently skipped (a stale group reference must
  not crash the scene).
* "device": MAC resolves via the device repository to a
  ``network_id``; unknown / unmigrated devices are skipped.
* Actions without a ``target`` (e.g. ``kind == "sync"``,
  ``kind == "delay"``) contribute nothing. They inherit the scope
  computed from the rest of the scene.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


def _network_id_from_device(controller, addr: str) -> Optional[str]:
    repo = getattr(controller, "device_repository", None)
    if repo is None:
        return None
    # Try the canonical lookups in order — production repo exposes
    # get_by_addr; tests sometimes only expose ``list``.
    dev = None
    get_by_addr = getattr(repo, "get_by_addr", None)
    if callable(get_by_addr):
        try:
            dev = get_by_addr(addr)
        except Exception:
            # swallow-ok: repository lookup is best-effort; a malformed
            # device entry must not crash the scene scope computation.
            dev = None
    if dev is None:
        get_dev = getattr(controller, "getDeviceFromAddress", None)
        if callable(get_dev):
            try:
                dev = get_dev(addr)
            except Exception:
                # swallow-ok: legacy controller fallback path, same
                # tolerance as the primary lookup above.
                dev = None
    if dev is None:
        return None
    nid = getattr(dev, "network_id", None)
    return str(nid) if nid else None


def _network_id_from_group(controller, group_id: int) -> Optional[str]:
    """Resolve a group_id to its ``network_id``. Returns ``None`` when
    the group is unknown, has no network binding, or the repository
    lookup raises — callers treat ``None`` as "skip silently"."""
    if group_id is None:
        return None
    try:
        gid_int = int(group_id) & 0xFF
    except (TypeError, ValueError):
        return None
    repo = getattr(controller, "group_repository", None)
    if repo is None:
        return None
    list_fn = getattr(repo, "list", None)
    if not callable(list_fn):
        return None
    try:
        groups = list(list_fn())
    except Exception:
        # swallow-ok: a malformed group repository must not crash the
        # scene scope computation — caller treats None as "skip".
        return None
    # Group_id maps to repo index in the project's convention (mirrors
    # the same pattern used by ``Controller.transport_for_group``).
    if 0 <= gid_int < len(groups):
        nid = getattr(groups[gid_int], "network_id", None)
        return str(nid) if nid else None
    return None


def _all_known_network_ids(controller) -> Tuple[str, ...]:
    """Every persisted ``RL_Network.id`` from the network repository."""
    repo = getattr(controller, "network_repository", None)
    if repo is None:
        return ()
    list_fn = getattr(repo, "list", None)
    if not callable(list_fn):
        return ()
    try:
        items = list(list_fn())
    except Exception:
        # swallow-ok: a malformed network repository must not crash the
        # scene scope computation — fall back to empty (callers treat
        # an empty network set as "no resolvable scope").
        return ()
    out: list = []
    for n in items:
        nid = getattr(n, "id", None)
        if nid:
            out.append(str(nid))
    return tuple(out)


def _network_ids_for_target(
    controller,
    target: Mapping[str, Any],
    *,
    parent_scope: Optional[Iterable[str]] = None,
) -> Iterable[str]:
    """Resolve one action's ``target`` to a (possibly empty) iterable
    of network ids. Helper for :func:`scene_network_ids`.

    ``parent_scope`` is the set of network ids the *enclosing* container
    action addresses. When the target is ``broadcast`` and a parent scope
    is supplied (i.e. this is a child action inside an ``offset_group``),
    "broadcast" means "the full scope of the parent action" — NOT every
    network. Only a top-level broadcast (``parent_scope is None``) keeps
    the historical fleet-wide meaning. ``groups`` / ``device`` targets
    resolve to their own networks regardless of the parent.
    """
    if not isinstance(target, Mapping):
        return ()
    kind = target.get("kind")
    if kind == "broadcast":
        if parent_scope is not None:
            return tuple(str(n) for n in parent_scope)
        return _all_known_network_ids(controller)
    if kind == "groups":
        out: list = []
        for gid in target.get("value") or ():
            nid = _network_id_from_group(controller, gid)
            if nid:
                out.append(nid)
        return out
    if kind == "device":
        addr = str(target.get("value") or "")
        if not addr:
            return ()
        nid = _network_id_from_device(controller, addr)
        return (nid,) if nid else ()
    return ()


def scene_network_ids(
    scene: Mapping[str, Any],
    *,
    controller: Any,
) -> Tuple[str, ...]:
    """Return the set of network ids the scene's broadcast actions reach.

    Precedence:

    1. If ``scene["network_scope"]`` is in explicit mode, return that
       persisted list filtered against currently-persisted networks
       (stale ids dropped silently with an INFO log). This is the
       operator-pinned override — explicit scope WINS over what the
       actions imply.
    2. Otherwise (auto mode or missing field), fall back to walking
       every action's ``target`` and resolving it against the device
       + group repositories. The returned tuple is ordered by first
       occurrence and deduplicated.

    Sub-actions inside ``offset_group`` containers are descended into
    (both modes): the container's own ``target`` AND each child's
    ``target`` both contribute.

    Empty tuple means "no addressable scope" — either the scene has
    only sync/delay actions, every target resolves to unknown
    devices/groups, OR the explicit scope's networks have all been
    deleted. The caller decides what to do (typically: no fan-out,
    runner records ``error="scope_resolved_empty"``).
    """
    if not isinstance(scene, Mapping):
        return ()

    # Tier 1: explicit scope wins. Soft-filter against the controller's
    # network repository so a network deleted after the scene was saved
    # disappears at runtime rather than tripping a routing error. If
    # all ids drop out we return ``()`` — the runner treats this as a
    # degraded run rather than silently widening back to fleet-wide.
    scope = scene.get("network_scope")
    if isinstance(scope, Mapping) and scope.get("mode") == "explicit":
        explicit_ids = scope.get("network_ids") or ()
        known = set(_all_known_network_ids(controller))
        out_explicit: list = []
        missing: list = []
        for nid in explicit_ids:
            s = str(nid)
            if not s:
                continue
            # When the controller has no network repo (test fakes /
            # standalone) we cannot validate — fall through and keep
            # the id verbatim.
            if known and s not in known:
                missing.append(s)
                continue
            if s not in out_explicit:
                out_explicit.append(s)
        if missing:
            logger.info(
                "scene_network_ids: explicit scope references missing "
                "networks: %r — soft-filtered out",
                missing,
            )
        return tuple(out_explicit)

    # Tier 2: auto / missing — walk actions as before.
    actions = scene.get("actions") or ()
    seen: set = set()
    ordered: list = []

    def _add(nid: Optional[str]) -> None:
        if not nid:
            return
        s = str(nid)
        if s in seen:
            return
        seen.add(s)
        ordered.append(s)

    def _walk(action: Mapping[str, Any], parent_scope: Optional[Tuple[str, ...]] = None) -> None:
        if not isinstance(action, Mapping):
            return
        target = action.get("target")
        if isinstance(target, Mapping):
            resolved = tuple(
                _network_ids_for_target(controller, target, parent_scope=parent_scope)
            )
            for nid in resolved:
                _add(nid)
            # This action's resolved networks become the scope its
            # children's "broadcast" targets inherit.
            child_scope: Optional[Tuple[str, ...]] = resolved
        else:
            # No target of its own (e.g. sync/delay) — children inherit
            # whatever scope this action was given.
            child_scope = parent_scope
        # offset_group + future container kinds: descend into children.
        for child in action.get("actions") or ():
            _walk(child, parent_scope=child_scope)

    for action in actions:
        _walk(action)

    return tuple(ordered)
