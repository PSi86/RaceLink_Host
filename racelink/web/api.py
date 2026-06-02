"""Flask API registration for the RaceLink web layer."""

from __future__ import annotations

import logging
import time

from flask import jsonify, request

# Module logger for the broad-except sweep (2026-04-27). ``ctx.log`` is
# also used for some operator-facing messages, but the broad-except
# blocks need full traceback + exception-type detail in the diagnostic
# log — that's what ``logger.exception`` and ``exc_info=True`` give us.
# A bare ``str(ex)`` previously hid an ``AttributeError`` for a renamed
# method behind a generic 500 for over a year (see
# ``api_devices_control``'s historical ``sendGroupControl`` reference).
logger = logging.getLogger(__name__)

from ..domain import (
    default_device_name,
    rl_preset_select_options,
    serialize_rl_preset_editor_schema,
    state_scope,
    wled_preset_select_options,
)
from ..domain.flags import USER_FLAG_DEFS
from ..domain.indicators import DEFAULT_INDICATE_DURATION_SEC, IndicatorType
from ..domain.network_boundary import (
    NetworkBoundaryViolation,
    SceneScopeViolation,
    validate_group_membership,
    validate_network_kind_match,
    validate_scene_scope_consistency,
)
from ..domain.node_config import serialize_node_config_schema
from ..services import OTAWorkflowService, SpecialsService
from ..services.scene_cost_estimator import estimate_scene, lora_parameters
from ..services.scenes_service import (
    GROUP_ID_MAX,
    KIND_OFFSET_GROUP,
    KIND_RL_EFFECT,
    KIND_RL_PRESET,
    KIND_STARTBLOCK,
    KIND_WLED_PRESET,
    MAX_GROUPS_OFFSET_ENTRIES,
    MAX_OFFSET_GROUP_CHILDREN,
    OFFSET_FORMULA_MODE_LABELS,
    OFFSET_GROUP_CHILD_KINDS,
    OFFSET_MS_MAX,
    OFFSET_MS_MIN,
    SceneService,
    get_action_kinds_metadata,
)
from .dto import group_caps_counts, group_counts, serialize_device
from .request_helpers import (
    RequestParseError,
    parse_recv3_from_addr,
    parse_wifi_options,
    require_int,
)


def _sse_refresh(ctx, scopes) -> None:
    """Broadcast an SSE ``refresh`` event derived from a state-scope set.

    Central helper so WebUI topics stay in sync with plugin-side scope tokens
    (one source of truth in :mod:`racelink.domain.state_scope`).
    """
    what = state_scope.sse_what_from_scopes(scopes)
    if not what:
        return
    ctx.sse.broadcast("refresh", {"what": what})


def _apply_device_meta_updates(
    ctx,
    *,
    macs: list,
    new_group,
    new_name,
    names: dict | None = None,
    progress_cb=None,
) -> dict:
    """Apply rename + regroup updates (plan P2-4, deadlock-fix; 2026-04-29 bulk-task refactor).

    **Important locking rule:** we must NOT hold ``ctx.rl_lock`` across the
    blocking ``setNodeGroupId`` call. That lock is the same one
    ``GatewayService.handle_ack_event`` acquires when the reply comes back
    over USB. If we hold it while waiting for the ACK, the reader thread
    stalls in ``handle_ack_event`` for the previous device, USB frames for
    the current device stack up in pyserial's RX buffer, and the current
    device times out even though its ACK is sitting in the queue.

    So the lock scope here is limited to the in-memory mutations. The TX
    itself runs lock-free (the transport has its own thread safety).

    **2026-04-29 fix.** Already-offline devices skip the SET_GROUP wire
    send entirely — the host's auto-restore mechanism
    (``gateway_service._restore_known_device_group``) pushes the new
    groupId on the device's next IDENTIFY/STATUS reply when it comes
    back online. Skipping eliminates the 8 s per-offline-device wait
    that the operator used to stare at with no UI feedback.

    ``progress_cb(index, total, mac, stage, message)`` is invoked
    once per iteration so the caller (a TaskManager runner) can
    update task meta + push a per-device SSE refresh. Pass ``None``
    for the legacy synchronous shape.

    Returns a dict ``{changed, skipped_offline, timed_out, total}``
    instead of the bare int the pre-2026-04-29 version returned, so
    the route can surface the operator-facing breakdown in the
    completion toast.
    """
    total = len(macs)
    changed = 0
    skipped_offline = 0
    timed_out = 0
    # Group ids whose membership changed in this batch — the group(s) a
    # device left and the group it joined. After the moves are applied we
    # recompute each one's network binding: the joined group inherits the
    # device's network (stamping its RF/Ethernet kind on first member), and
    # any group left empty reverts to network-agnostic.
    affected_gids: set[int] = set()
    for index, mac in enumerate(macs, start=1):
        if progress_cb:
            progress_cb(index, total, mac, "MOVING", f"Moving {mac} → group {new_group}")
        with ctx.rl_lock:
            dev = ctx.rl_instance.getDeviceFromAddress(mac)
            if dev is None:
                continue
            # Bulk-rename path: per-MAC names supplied by the
            # ``BulkRenameDialog`` after pattern expansion. An empty
            # string is the explicit reset marker — restore the default
            # ``"WLED <mac12>"`` shape used by the IDENTIFY path.
            if names is not None and mac in names:
                raw = names.get(mac)
                resolved = raw.strip() if isinstance(raw, str) else ""
                dev.name = resolved if resolved else default_device_name(mac)
                changed += 1
            elif new_name is not None and isinstance(new_name, str) and len(macs) == 1:
                # Single-rename inline-edit path. Empty input is the
                # reset marker — same semantics as the bulk path so the
                # operator can revert an individual device by clearing
                # its inline-edit field.
                stripped = new_name.strip()
                dev.name = stripped if stripped else default_device_name(mac)
                changed += 1
            if new_group is None:
                continue
            try:
                old_gid = int(getattr(dev, "groupId", 0) or 0)
            except (TypeError, ValueError):
                old_gid = 0
            dev.groupId = int(new_group)
            affected_gids.add(old_gid)
            affected_gids.add(int(new_group))
            was_online = bool(getattr(dev, "link_online", False))
        # Lock released -- the reader thread can now drain ACKs from the
        # previous iteration and complete matches for the *current* one.
        if not was_online:
            # Skip the wire send for already-offline devices. The host-
            # side groupId is updated; the auto-restore mechanism pushes
            # SET_GROUP on the device's next reply.
            skipped_offline += 1
            continue
        try:
            ok = ctx.rl_instance.setNodeGroupId(dev)
            if ok:
                changed += 1
            else:
                # ``setNodeGroupId`` already called ``mark_offline`` on
                # timeout (controller.py); we just count it for the
                # operator-facing summary.
                timed_out += 1
        except Exception as ex:
            # swallow-ok: bulk update keeps trying the remaining macs.
            # The exception type (TimeoutError vs AttributeError vs
            # SerialException) is critical for diagnosis — the previous
            # ``{ex}`` formatting only carried the message string.
            timed_out += 1
            ctx.log(
                f"RaceLink: setNodeGroupId failed for {mac}: "
                f"{type(ex).__name__}: {ex}"
            )
            logger.warning(
                "setNodeGroupId failed for %s", mac, exc_info=True,
            )
    # Re-derive each touched group's network binding now that every move
    # is applied. ``reconcile_group_network`` ignores Unconfigured (0) and
    # static groups; a joined group inherits the member's network, an
    # emptied group falls back to ``None`` (network-agnostic).
    if affected_gids:
        with ctx.rl_lock:
            for gid in affected_gids:
                try:
                    ctx.rl_instance.reconcile_group_network(gid)
                except Exception:
                    logger.warning(
                        "reconcile_group_network failed for group %s", gid,
                        exc_info=True,
                    )
    return {
        "changed": changed,
        "skipped_offline": skipped_offline,
        "timed_out": timed_out,
        "total": total,
    }


def _iterate_force_groups(
    ctx,
    *,
    sanity_check: bool = True,
    skip_offline: bool = False,
    progress_cb=None,
) -> dict:
    """Re-broadcast every device's stored groupId to the network.

    Sibling of :func:`_apply_device_meta_updates`. Where the
    bulk-set helper mutates the groupId based on operator input
    and pushes the new value, this helper iterates the existing
    repository and re-pushes whatever each device already has —
    used to recover from a host/firmware groupId mismatch (the
    "Re-sync group config" operator action).

    ``skip_offline`` (default ``False``): when ``True``, devices
    whose ``link_online`` flag is False are skipped entirely. The
    auto-restore mechanism
    (:meth:`GatewayService._restore_known_device_group`) pushes
    SET_GROUP on the device's next IDENTIFY/STATUS reply when it
    returns. Default is ``False`` — re-sync's operator semantic
    is "push to *all* devices, including the flaky ones"; the
    bulk-set sibling defaults to skip-offline because that
    operator semantic is "I'm reorganising; the offline ones
    can wait". The web route exposes the toggle so the operator
    can pick the appropriate mode.

    Sanity check (when ``sanity_check=True``) clamps any device
    whose stored groupId points at a deleted group back to 0
    (Unconfigured). Mirrors the legacy :meth:`RL.forceGroups`
    behaviour. Runs regardless of ``skip_offline`` — an offline
    device with a stale groupId still gets its in-memory state
    fixed; auto-restore pushes the correction on its next reply.

    Returns ``{changed, skipped_offline, timed_out, total}``
    matching the bulk-set helper's shape so the same frontend
    summary toast renders unchanged.
    """
    with ctx.rl_lock:
        devices_snapshot = list(ctx.rl_instance.device_repository.list())
        num_groups = len(ctx.rl_instance.group_repository.list())
    total = len(devices_snapshot)
    changed = 0
    skipped_offline = 0
    timed_out = 0
    for index, dev in enumerate(devices_snapshot, start=1):
        addr = getattr(dev, "addr", "?") or "?"
        if progress_cb:
            progress_cb(
                index, total, addr, "RESYNC",
                f"Re-sync {addr} → group {int(getattr(dev, 'groupId', 0) or 0)}",
            )
        with ctx.rl_lock:
            if sanity_check and int(getattr(dev, "groupId", 0) or 0) >= num_groups:
                dev.groupId = 0
            was_online = bool(getattr(dev, "link_online", False))
        if skip_offline and not was_online:
            skipped_offline += 1
            continue
        try:
            ok = ctx.rl_instance.setNodeGroupId(dev)
            if ok:
                changed += 1
            else:
                timed_out += 1
        except Exception as ex:
            # swallow-ok: re-sync iterates every known device; a single
            # device's failure shouldn't stop the rest. The exception
            # type is logged so a recurring transport bug is diagnosable.
            timed_out += 1
            ctx.log(
                f"RaceLink: setNodeGroupId failed for {addr}: "
                f"{type(ex).__name__}: {ex}"
            )
            logger.warning(
                "setNodeGroupId failed for %s during force_groups", addr,
                exc_info=True,
            )
    return {
        "changed": changed,
        "skipped_offline": skipped_offline,
        "timed_out": timed_out,
        "total": total,
    }


def _prepare_discover_target(ctx, *, target_gid, new_group_name):
    """Create a group if requested and return ``(target_gid, created_gid)``.

    Extracted from ``api_discover`` (plan P2-4) so the locking+group-creation
    logic can be unit-tested without a Flask request context.
    """
    created_gid = None
    # A discover-created group is network-agnostic (``network_id=None``)
    # like any other new group: the discovered devices that land in it
    # stamp its network via ``reconcile_group_network``, so the group's
    # RF/Ethernet kind is decided by its members, not by create-time
    # defaulting.
    with ctx.rl_lock:
        if new_group_name:
            group = ctx.RL_DeviceGroup(
                str(new_group_name), static_group=0, dev_type=0,
                network_id=None,
            )
            if ctx.group_repo is not None:
                created_gid = ctx.group_repo.append(group)
            else:
                ctx.rl_grouplist.append(group)
                created_gid = len(ctx.rl_grouplist) - 1
            ctx.log(f"RaceLink: Created group '{new_group_name}' (id={created_gid})")
        # A freshly-created group is the explicit destination for the
        # discovered devices and takes precedence over the "Add discovered
        # to" selector. The dialog always sends a ``targetGroupId`` (default
        # Unconfigured = 0), so a ``target_gid is None`` guard never fired
        # when a name was typed — the group got created but the devices were
        # dropped into the selector's group (e.g. Unconfigured) instead.
        if created_gid is not None:
            target_gid = created_gid
    return target_gid, created_gid


def _resolve_special_config_request(ctx, body, specials_service):
    """Parse+validate a ``/api/specials/config`` body. Returns ``(ok, payload, status)``.

    On success, ``payload`` is a dict with the validated request data; on
    failure, ``payload`` is an error dict and ``status`` is the HTTP code.
    Extracted from ``api_specials_config`` (plan P2-4).

    Accepts ``value`` as either an int (scalar 1/2-byte options) or a
    dict (``{start, stop}`` for ``shape == "uint16-pair"``). The packed
    ``data0..3`` bytes are returned in the payload so the route handler
    can forward them straight to ``sendConfig``.
    """
    mac = body.get("mac", None)
    key = body.get("key", None)
    value = body.get("value", None)
    if not mac or not key:
        return False, {"ok": False, "error": "missing mac/key"}, 400

    recv3 = parse_recv3_from_addr(mac)
    if not recv3:
        return False, {"ok": False, "error": "invalid mac/address"}, 400
    if recv3 == b"\xFF\xFF\xFF":
        return False, {"ok": False, "error": "broadcast not allowed for config"}, 400

    mac_str = str(mac).upper()
    with ctx.rl_lock:
        dev = ctx.rl_instance.getDeviceFromAddress(mac_str)
        if not dev:
            return False, {"ok": False, "error": "device not found"}, 404
        option_info = specials_service.resolve_option(dev, key)

    if not option_info:
        return False, {"ok": False, "error": "option not supported for device"}, 400
    option = option_info.get("option", None)
    if option is None:
        return False, {"ok": False, "error": "option not writable"}, 400
    try:
        specials_service.validate_option_value(option_info, value)
    except ValueError as ex:
        return False, {"ok": False, "error": str(ex)}, 400

    try:
        d0, d1, d2, d3 = specials_service.pack_option_value(option_info, value)
    except ValueError as ex:
        return False, {"ok": False, "error": str(ex)}, 400

    return True, {
        "mac_str": mac_str,
        "key": key,
        "recv3": recv3,
        "option": option,
        "option_info": option_info,
        "value": value,
        "data0": d0,
        "data1": d1,
        "data2": d2,
        "data3": d3,
    }, 200


def _gateway_status(ctx) -> dict:
    """Return a UI-friendly gateway readiness snapshot (plan P1-1)."""
    rl = ctx.rl_instance
    getter = getattr(rl, "gateway_status", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            # swallow-ok: fall through to the synthetic snapshot rather
            # than 500-ing the /api/master endpoint. A failing getter is
            # a real bug though — log with full traceback so a recurring
            # failure surfaces in the diagnostic log, not silently in
            # corrupted master state.
            logger.exception("gateway_status getter raised; using synthetic fallback")
    return {
        "ready": bool(getattr(rl, "ready", False)),
        "last_error": None,
        "failure_count": 0,
    }


def register_api_routes(bp, ctx):
    host_wifi_service = ctx.services["host_wifi"]
    ota_service = ctx.services["ota"]
    presets_service = ctx.services["presets"]
    rl_presets_service = ctx.services.get("rl_presets")
    scenes_service = ctx.services.get("scenes")
    scene_runner_service = ctx.services.get("scene_runner")
    host_settings_service = ctx.services.get("host_settings")
    specials_service = SpecialsService(rl_instance=ctx.rl_instance)
    ota_workflows = OTAWorkflowService(
        host_wifi_service=host_wifi_service,
        ota_service=ota_service,
        presets_service=presets_service,
    )


    @bp.route("/api/devices", methods=["GET"])
    def api_devices():
        with ctx.rl_lock:
            rows = [
                serialize_device(device, battery_helper=host_settings_service)
                for device in ctx.devices()
            ]
        return jsonify({"ok": True, "devices": rows})

    @bp.route("/api/specials", methods=["GET"])
    def api_specials():
        return jsonify({"ok": True, "specials": specials_service.get_serialized_config()})

    @bp.route("/api/groups", methods=["GET"])
    def api_groups():
        with ctx.rl_lock:
            devices = ctx.devices()
            counts = group_counts(devices)
            # C5: per-group capability counts (e.g. ``{"WLED": 3,
            # "STARTBLOCK": 1}``) so the scene editor can filter
            # dropdowns to groups that actually have devices the
            # action's wire packet would land on.
            caps_counts = group_caps_counts(devices)
            rows = [{
                "id": 0,
                "name": "Unconfigured",
                "static": False,
                "dev_type": 0,
                "device_count": int(counts.get(0, 0)),
                "caps_in_group": dict(caps_counts.get(0, {})),
                # Stage 4: Unconfigured is the cross-network sink —
                # devices of any network can land here. Surface as
                # ``null`` so the WebUI knows not to render a badge.
                "network_id": None,
            }]
            for gid, group in enumerate(ctx.groups()):
                name = getattr(group, "name", f"Group {gid}")
                if str(name).strip().lower() in {"unconfigured", "all wled nodes", "all wled devices"}:
                    continue
                rows.append({
                    "id": gid,
                    "name": name,
                    "static": bool(getattr(group, "static_group", 0)),
                    "dev_type": int(getattr(group, "dev_type", 0) or 0),
                    "device_count": int(counts.get(gid, 0)),
                    "caps_in_group": dict(caps_counts.get(gid, {})),
                    "network_id": (
                        str(getattr(group, "network_id", "") or "") or None
                    ),
                })
        return jsonify({"ok": True, "groups": rows})

    @bp.route("/api/master", methods=["GET"])
    def api_master():
        # Stage 2 Part 4: the ``master`` field carries the multi-network
        # payload ``{networks: [...], default_network_id: "..."}``. At
        # N=1 attached gateway the array has a single entry, which the
        # legacy frontend reads as ``networks[0]`` to preserve the
        # single-master UX. Stage 4 introduces a proper multi-network
        # UI driven by the same payload.
        gateway = _gateway_status(ctx)
        return jsonify({
            "ok": True,
            "master": ctx.sse.masters.snapshot(),
            "task": ctx.tasks.snapshot(),
            "gateway": gateway,
        })

    @bp.route("/api/networks", methods=["GET"])
    def api_networks():
        """Read-only listing of the operator's configured RaceLink
        networks plus their current live state (Stage 2 Part 4).

        Stage 3 adds CRUD; for now this exposes the repository so the
        WebUI can render network badges / filters on the device table.
        Each row carries the persisted metadata (id, name,
        gateway_mac, region, channel_id, rf_config) plus the live
        :class:`MasterState` snapshot for the same network id.
        """
        rl = ctx.rl_instance
        net_repo = getattr(rl, "network_repository", None)
        masters_snap = ctx.sse.masters.snapshot() if ctx.sse else {"networks": [], "default_network_id": None}
        live_by_id = {
            row.get("network_id"): row
            for row in masters_snap.get("networks", [])
            if isinstance(row, dict) and row.get("network_id")
        }
        rows = []
        if net_repo is not None:
            try:
                items = list(net_repo.list())
            except Exception:
                logger.exception("/api/networks: repo iteration raised")
                items = []
            for net in items:
                nid = str(getattr(net, "id", "") or "")
                rows.append({
                    "id": nid,
                    "name": str(getattr(net, "name", "") or ""),
                    "kind": str(getattr(net, "kind", "rf") or "rf"),
                    "gateway_mac": getattr(net, "gateway_mac", None),
                    "region": getattr(net, "region", None),
                    "channel_id": getattr(net, "channel_id", None),
                    "rf_config": getattr(net, "rf_config", None),
                    "eth_config": getattr(net, "eth_config", None),
                    "created_ts": getattr(net, "created_ts", None),
                    "live": live_by_id.get(nid),
                })
        return jsonify({
            "ok": True,
            "networks": rows,
            "default_network_id": masters_snap.get("default_network_id"),
        })

    @bp.route("/api/networks", methods=["POST"])
    def api_network_create():
        """Create a new Ethernet-kind network (Ethernet PoC).

        RF networks are created via the gateway-bind wizard (they need a
        probed ``rf_config``); this endpoint is the lightweight seed for an
        IP/LAN network. Body::

            {name, kind:"ethernet", node_port?, host_port?, bind_host?,
             broadcast_host?, discovery?}

        On success the network is persisted and its ``EthernetTransport`` is
        attached immediately (no full gateway rediscover needed).
        """
        from ..domain.models import RL_Network

        rl = ctx.rl_instance
        net_repo = getattr(rl, "network_repository", None)
        if net_repo is None:
            return jsonify({"ok": False, "error": "network repository unavailable"}), 503
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "name required"}), 400
        kind = str(body.get("kind") or "ethernet").strip().lower()
        if kind != "ethernet":
            return jsonify({
                "ok": False,
                "error": "only kind='ethernet' networks can be created here "
                         "(RF networks are created via the gateway bind wizard)",
            }), 400

        def _int(key, default):
            try:
                return int(body.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        eth_config = {
            "node_port": _int("node_port", 5078),
            "host_port": _int("host_port", 5079),
            "bind_host": str(body.get("bind_host") or "0.0.0.0"),
            "broadcast_host": str(body.get("broadcast_host") or "255.255.255.255"),
            "discovery": str(body.get("discovery") or "broadcast"),
        }
        net = RL_Network(name=name, kind="ethernet", eth_config=eth_config)
        with ctx.rl_lock:
            net_repo.append(net)
        try:
            rl.save_to_db({"manual": True}, scopes={state_scope.FULL})
        except Exception:
            logger.exception("api_network_create: save_to_db failed")

        # Attach the Ethernet transport now so discovery can run immediately.
        # Best-effort + idempotent (the attach path dedupes on the ETH ident).
        attach = getattr(rl, "_attach_ethernet_transports", None)
        if callable(attach):
            try:
                attach()
            except Exception:
                logger.exception("api_network_create: ethernet transport attach raised")

        _sse_refresh(ctx, {state_scope.FULL})
        return jsonify({
            "ok": True,
            "network": {
                "id": net.id,
                "name": net.name,
                "kind": net.kind,
                "gateway_mac": None,
                "region": None,
                "channel_id": None,
                "rf_config": None,
                "eth_config": net.eth_config,
                "created_ts": net.created_ts,
            },
        })

    @bp.route("/api/networks/<network_id>", methods=["PUT"])
    def api_network_update(network_id: str):
        """Stage 4 Block 3: rename a network and/or change its
        region+channel binding.

        Body shape: ``{name?: str, region?: str, channel_id?:
        int|null, rf_config?: dict|null}``. Channel-driven updates
        rewrite ``rf_config`` to the channel-table entry's seven
        wire fields (so the network and the bound gateway speak
        the same RF); explicit ``rf_config`` overrides the channel
        lookup for the Advanced-Mode operator who types raw values.

        Returns HTTP 400 on validation failure (unknown channel id,
        bad region, separation conflict). Persists on success.
        """
        from ..domain.rf_channels import channel_rf_config, list_channels
        from ..domain.rf_policy import (
            format_conflict, validate_networks_separation,
        )

        rl = ctx.rl_instance
        net_repo = getattr(rl, "network_repository", None)
        if net_repo is None:
            return jsonify({"ok": False, "error": "network repository unavailable"}), 503
        net = net_repo.get_by_id(network_id)
        if net is None:
            return jsonify({"ok": False, "error": f"unknown network_id {network_id!r}"}), 404

        body = request.get_json(silent=True) or {}
        # Name --------------------------------------------------------
        new_name = body.get("name", None)
        if new_name is not None:
            new_name = str(new_name).strip()
            if not new_name:
                return jsonify({"ok": False, "error": "name cannot be empty"}), 400
        # Region ------------------------------------------------------
        new_region = body.get("region", None)
        if new_region is not None:
            new_region = str(new_region).strip()
            if not list_channels(new_region):
                return jsonify({
                    "ok": False,
                    "error": f"unknown region {new_region!r}",
                }), 400
        effective_region = new_region or getattr(net, "region", None) or "EU868"
        # Channel + rf_config ----------------------------------------
        new_channel_id = body.get("channel_id", "__missing__")
        explicit_rf_config = body.get("rf_config", "__missing__")
        new_rf_config = None
        if explicit_rf_config != "__missing__":
            if explicit_rf_config is None:
                new_rf_config = None
            elif isinstance(explicit_rf_config, dict):
                # Best-effort coercion; the bind/migration validators
                # also normalise the dict before consuming it.
                new_rf_config = dict(explicit_rf_config)
            else:
                return jsonify({
                    "ok": False,
                    "error": "rf_config must be an object",
                }), 400
        elif new_channel_id != "__missing__":
            if new_channel_id is None:
                new_rf_config = None
            else:
                try:
                    ch_id_int = int(new_channel_id)
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": "channel_id must be an integer"}), 400
                resolved = channel_rf_config(effective_region, ch_id_int)
                if resolved is None:
                    return jsonify({
                        "ok": False,
                        "error": f"unknown channel_id {ch_id_int} in region {effective_region!r}",
                    }), 400
                new_rf_config = resolved
        # Speculatively apply on a shallow snapshot so the separation
        # check sees the would-be state, then run the validator across
        # every network. Reject before mutating if the policy fails.
        if new_rf_config is not None:
            class _SpecView:
                def __init__(self, real, rf_config):
                    self._real = real
                    self.rf_config = rf_config

                def __getattr__(self, name):
                    return getattr(self._real, name)

            spec = _SpecView(net, new_rf_config)
            others = [n for n in net_repo.list() if n is not net]
            conflicts = validate_networks_separation([spec, *others])
            if conflicts:
                return jsonify({
                    "ok": False,
                    "error": format_conflict(conflicts[0]),
                    "detail": {"code": "rf_separation", "conflicts": conflicts},
                }), 400
        # Commit ------------------------------------------------------
        if new_name is not None:
            net.name = new_name
        if new_region is not None:
            net.region = new_region
        if new_channel_id != "__missing__":
            try:
                net.channel_id = int(new_channel_id) if new_channel_id is not None else None
            except (TypeError, ValueError):
                net.channel_id = None
        if new_rf_config is not None or explicit_rf_config is None and new_channel_id is None:
            net.rf_config = dict(new_rf_config) if new_rf_config is not None else None
        try:
            rl.save_to_db({"manual": True}, scopes={state_scope.FULL})
        except Exception:
            logger.exception("api_network_update: save_to_db failed")
        # Push the refreshed list to every connected SSE client so
        # the WebUI's networks store + DeviceTable badge re-render
        # without a manual refresh.
        _sse_refresh(ctx, {state_scope.FULL})
        return jsonify({
            "ok": True,
            "network": {
                "id": str(net.id),
                "name": str(net.name),
                "region": getattr(net, "region", None),
                "channel_id": getattr(net, "channel_id", None),
                "rf_config": getattr(net, "rf_config", None),
                "gateway_mac": getattr(net, "gateway_mac", None),
            },
        })

    @bp.route("/api/networks/<network_id>", methods=["DELETE"])
    def api_network_delete(network_id: str):
        """Stage 4 Block 3: delete a network record.

        Refuses when:
          * the network is the only one left (the device repo and
            v1→v2 migration both assume at least one network exists);
          * any device still references it via ``network_id``;
          * any group still references it via ``network_id``.

        On success the WebUI re-fetches /api/networks via the
        broadcast refresh and the gateway-bind state machine will
        re-evaluate any transport whose ident_mac matched the
        deleted network's ``gateway_mac``.
        """
        rl = ctx.rl_instance
        net_repo = getattr(rl, "network_repository", None)
        if net_repo is None:
            return jsonify({"ok": False, "error": "network repository unavailable"}), 503
        net = net_repo.get_by_id(network_id)
        if net is None:
            return jsonify({"ok": False, "error": f"unknown network_id {network_id!r}"}), 404
        with ctx.rl_lock:
            if len(net_repo.list()) <= 1:
                return jsonify({
                    "ok": False,
                    "error": "cannot delete the last network — at least one must exist",
                }), 400
            device_refs = [
                str(getattr(d, "addr", "") or "")
                for d in ctx.devices()
                if str(getattr(d, "network_id", "") or "") == str(network_id)
            ]
            if device_refs:
                return jsonify({
                    "ok": False,
                    "error": (
                        f"{len(device_refs)} device(s) still reference this "
                        "network. Migrate or re-assign them first."
                    ),
                    "detail": {"code": "devices_attached", "device_macs": device_refs[:8]},
                }), 400
            group_refs = [
                str(getattr(g, "name", "") or "")
                for g in ctx.groups()
                if str(getattr(g, "network_id", "") or "") == str(network_id)
            ]
            if group_refs:
                return jsonify({
                    "ok": False,
                    "error": (
                        f"{len(group_refs)} group(s) still belong to this "
                        "network. Reassign them first."
                    ),
                    "detail": {"code": "groups_attached", "group_names": group_refs[:8]},
                }), 400
            net_repo.remove(net)
        try:
            rl.save_to_db({"manual": True}, scopes={state_scope.FULL})
        except Exception:
            logger.exception("api_network_delete: save_to_db failed")
        # Drop the bind record for the (possibly now-orphaned) gateway
        # so the next attach cycle starts clean.
        bind_service = getattr(rl, "gateway_bind_service", None)
        gw_mac = str(getattr(net, "gateway_mac", "") or "")
        if bind_service is not None and gw_mac:
            try:
                bind_service.forget(gw_mac)
            except Exception:
                logger.exception(
                    "api_network_delete: bind_service.forget raised",
                )
        _sse_refresh(ctx, {state_scope.FULL})
        return jsonify({"ok": True, "deleted_id": str(network_id)})

    @bp.route("/api/channels", methods=["GET"])
    def api_channels():
        """Stage 4: shipped region/channel lookup table.

        Returns ``{"ok": True, "regions": {"EU868": [{id, name,
        freq_hz, ...}, ...], "US915": [...]}}``. Drives the WebUI's
        Network Manager channel dropdown + the Channel Scan
        wizard's channel-selection checkbox list. The table is a
        compile-time constant on the server (see
        :mod:`racelink.domain.rf_channels`); it doesn't need
        per-request server work and the WebUI can cache the response
        for the session.
        """
        from ..domain.rf_channels import REGION_CHANNELS
        return jsonify({
            "ok": True,
            "regions": {
                region: [dict(ch) for ch in channels]
                for region, channels in REGION_CHANNELS.items()
            },
        })

    @bp.route("/api/gateways", methods=["GET"])
    def api_gateways_list():
        """Stage 3 Part D: snapshot of the bind-state machine.

        Returns ``{"ok": True, "gateways": [{ident_mac, state, ...},
        ...]}``. Drives the WebUI's per-gateway status bar + opens
        the bind wizard when any record is in ``conflict`` or
        ``unbound``.
        """
        rl = ctx.rl_instance
        bind_service = getattr(rl, "gateway_bind_service", None)
        gateways: list = []
        if bind_service is not None:
            snap = bind_service.snapshot()
            gateways = list(snap.get("gateways", []) or [])

        # Surface attached Ethernet transports as ready "gateways" so the
        # WebUI's per-gateway status bar renders a pill for them just like an
        # RF gateway. There is no bind state machine for Ethernet (the host
        # NIC is the transport, no gateway MAC), so the record is synthesised
        # as a permanently-``bound`` entry; its RF/link state comes from the
        # per-network MasterState (constant IDLE) via the ``master`` payload.
        net_repo = getattr(rl, "network_repository", None)
        for t in (getattr(rl, "transports", None) or []):
            if getattr(t, "kind", "rf") != "ethernet":
                continue
            nid = str(getattr(t, "network_id", "") or "")
            ident = str(getattr(t, "ident_mac", "") or "") or (f"ETH:{nid}" if nid else "")
            if not ident:
                continue
            name = None
            if net_repo is not None and nid:
                try:
                    net = net_repo.get_by_id(nid)
                    name = str(getattr(net, "name", "") or "") or None
                except Exception:
                    # swallow-ok: the label is cosmetic — fall back to the
                    # ident if the repo lookup hiccups.
                    name = None
            gateways.append({
                "ident_mac": ident,
                "state": "bound",
                "network_id": nid or None,
                "network_name": name,
                "rf_config_actual": None,
                "rf_config_expected": None,
                "conflict_fields": [],
                "migration_pending": False,
                "last_evaluated_ts": 0.0,
                "token": "",
                "kind": "ethernet",
            })
        return jsonify({"ok": True, "gateways": gateways})

    @bp.route("/api/networks/<network_id>/migrate", methods=["POST"])
    def api_network_migrate(network_id: str):
        """Stage 3 Part E: kick off the RF-migration task for one
        network.

        Body: ``{target_rf_config: {...}, force_offline?: bool}``.
        Returns ``{ok, task}`` immediately; live progress streams on
        the existing SSE ``task`` channel. The migration engine
        re-evaluates the bind service when done so the WebUI's
        ``gateway_bound``/``gateway_conflict`` indicators close out
        automatically.
        """
        migration = getattr(ctx.rl_instance, "rf_migration_service", None)
        if migration is None:
            return jsonify({"ok": False, "error": "migration service unavailable"}), 503
        body = request.get_json(silent=True) or {}
        target = body.get("target_rf_config")
        if not isinstance(target, dict):
            return jsonify({
                "ok": False,
                "error": "target_rf_config required (object with the P_RfConfig fields)",
            }), 400
        force_offline = bool(body.get("force_offline", False))
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        ctx.sse.ensure_transport_hooked(ctx.rl_instance)

        def _progress(payload):
            # Surface phase + per-device telemetry through the task
            # meta channel so the WebUI's migration wizard can render
            # a per-phase progress bar.
            ctx.tasks.update(meta=dict(payload))

        def _runner():
            return migration.migrate_network_to(
                network_id=network_id,
                target_rf_config=target,
                force_offline=force_offline,
                progress_cb=_progress,
            )

        task = ctx.tasks.start(
            "rf_migration", _runner,
            meta={
                "stage": "INIT",
                "network_id": network_id,
                "force_offline": force_offline,
            },
        )
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    @bp.route("/api/groups/migrate-network", methods=["POST"])
    def api_groups_migrate_network():
        """Move one or more groups (with all their members) onto a
        target network. Single TaskManager job; one combined progress
        stream for the multi-group case.

        Body shape::

            {
              "group_ids": [int, int, ...],
              "target_network_id": "net-uuid",
              "offline_mode": "block" | "skip" | "force"
            }

        Offline-mode semantics (mirrors the existing group-move
        ``_apply_device_meta_updates`` pattern):

        * ``block`` (default) — synchronous pre-check across every
          requested group's members; HTTP 400 with structured
          ``detail.offline_macs`` if any are offline.
        * ``skip`` — metadata flip only for offline devices; wire
          push for online ones. Channel Scan recovers offline
          devices later.
        * ``force`` — attempt the wire push for offline devices too;
          metadata flips regardless of wire outcome.

        Per-device + per-group metadata flips happen regardless of
        partial wire failure — operator intent ("these groups now
        belong to network B") governs. Stragglers surface in
        ``result["stranded"]``.

        Single-group move = list with one id; the body validation
        rejects an empty list with HTTP 400.
        """
        migration = getattr(ctx.rl_instance, "rf_migration_service", None)
        if migration is None:
            return jsonify({"ok": False, "error": "migration service unavailable"}), 503
        body = request.get_json(silent=True) or {}
        raw_gids = body.get("group_ids")
        if not isinstance(raw_gids, list) or not raw_gids:
            return jsonify({
                "ok": False,
                "error": "group_ids required (non-empty list of group ids)",
            }), 400
        target_network_id = body.get("target_network_id")
        if not isinstance(target_network_id, str) or not target_network_id.strip():
            return jsonify({
                "ok": False,
                "error": "target_network_id required",
            }), 400
        offline_mode = str(body.get("offline_mode") or "block").lower().strip()
        if offline_mode not in {"block", "skip", "force"}:
            return jsonify({
                "ok": False,
                "error": "offline_mode must be 'block', 'skip' or 'force'",
            }), 400

        # Dedupe + coerce the group_ids list early so the block-check
        # and the runner see the same set.
        clean_gids: list = []
        seen_gids: set = set()
        for raw in raw_gids:
            try:
                gid = int(raw) & 0xFF
            except (TypeError, ValueError):
                continue
            if gid in seen_gids:
                continue
            seen_gids.add(gid)
            clean_gids.append(gid)
        if not clean_gids:
            return jsonify({
                "ok": False,
                "error": "group_ids must contain at least one integer",
            }), 400

        # Resolve members across every requested group + offline
        # pre-check. Unknown ids → 404 fast (mirrors the service's
        # validate stage).
        member_macs: list = []
        offline_macs: list = []
        seen_macs: set = set()
        with ctx.rl_lock:
            groups_list = list(ctx.rl_instance.group_repository.list() or ())
            unknown: list = [
                gid for gid in clean_gids
                if not (0 <= gid < len(groups_list))
            ]
            if unknown:
                return jsonify({
                    "ok": False,
                    "error": f"unknown group_id(s): {unknown}",
                }), 404

            # Cross-kind guard: a group on an RF network cannot migrate onto
            # an Ethernet network (or vice versa). Reject synchronously with
            # HTTP 400 before launching the task — clearer than letting the
            # RF-specific migration fail mid-flight. Best-effort: target
            # existence is left to the service (which returns a failed task
            # for an unknown id); this only fires when we can resolve both
            # ends, and rf_migration_service.migrate_groups_to is the
            # authoritative backstop.
            net_repo = getattr(ctx.rl_instance, "network_repository", None)
            target_net = net_repo.get_by_id(target_network_id) if net_repo else None
            if target_net is not None:
                source_ids = {
                    str(getattr(groups_list[gid], "network_id", "") or "")
                    for gid in clean_gids
                }
                source_ids.discard("")
                try:
                    validate_network_kind_match(
                        target_net,
                        [net_repo.get_by_id(nid) for nid in source_ids],
                    )
                except NetworkBoundaryViolation as exc:
                    return jsonify({
                        "ok": False,
                        "error": exc.reason,
                        "detail": exc.detail,
                    }), 400

            gid_set = set(clean_gids)
            for dev in (ctx.rl_instance.device_repository.list() or ()):
                if int(getattr(dev, "groupId", 0) or 0) not in gid_set:
                    continue
                mac = str(getattr(dev, "addr", "") or "").upper()
                if not mac or mac in seen_macs:
                    continue
                seen_macs.add(mac)
                member_macs.append(mac)
                if not bool(getattr(dev, "link_online", False)):
                    offline_macs.append(mac)

        # Empty-membership case is allowed (single-group migration of
        # an empty group still flips group.network_id so the operator
        # can populate it on the target). Block-mode with offline
        # members still rejects.
        if offline_mode == "block" and offline_macs:
            return jsonify({
                "ok": False,
                "error": (
                    f"{len(offline_macs)} of {len(member_macs)} group member(s) "
                    "offline — bring them online first, or pass "
                    "offline_mode='skip' / 'force'."
                ),
                "detail": {
                    "code": "offline_block",
                    "offline_macs": offline_macs,
                },
            }), 400
        if offline_mode == "block":
            offline_mode = "skip"

        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        ctx.sse.ensure_transport_hooked(ctx.rl_instance)

        def _progress(payload):
            meta = dict(payload)
            meta["group_ids"] = list(clean_gids)
            ctx.tasks.update(meta=meta)

        def _runner():
            outcome = migration.migrate_groups_to(
                target_network_id=target_network_id,
                group_ids=clean_gids,
                offline_mode=offline_mode,
                progress_cb=_progress,
            )
            _sse_refresh(
                ctx,
                {state_scope.DEVICES, state_scope.DEVICE_MEMBERSHIP, state_scope.GROUPS},
            )
            return outcome

        task = ctx.tasks.start(
            "migrate_groups_to_network", _runner,
            meta={
                "stage": "INIT",
                "group_ids": list(clean_gids),
                "total": len(member_macs),
                "target_network_id": target_network_id,
                "offline_mode": offline_mode,
                "message": (
                    f"Migrating {len(clean_gids)} group"
                    f"{'s' if len(clean_gids) != 1 else ''} "
                    f"({len(member_macs)} device"
                    f"{'s' if len(member_macs) != 1 else ''}) "
                    f"→ network {target_network_id}…"
                ),
            },
        )
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    @bp.route("/api/gateways/<ident_mac>/channel-scan", methods=["POST"])
    def api_gateway_channel_scan(ident_mac: str):
        """Stage 3 Part F: walk a region's channel table on this
        gateway and report who answers per channel.

        Body: ``{region: str, channel_ids?: [int], identify_dwell_s?: float}``.
        Returns ``{ok, task}`` immediately; live per-channel
        progress streams on the existing SSE ``task`` channel. The
        task result carries the per-channel responders + the
        union (``all_known``, ``all_unknown``) the WebUI's
        wizard renders.
        """
        scan = getattr(ctx.rl_instance, "channel_scan_service", None)
        if scan is None:
            return jsonify({"ok": False, "error": "channel scan service unavailable"}), 503
        body = request.get_json(silent=True) or {}
        region = str(body.get("region") or "").strip()
        if not region:
            return jsonify({"ok": False, "error": "region required"}), 400
        channel_ids = body.get("channel_ids")
        if channel_ids is not None and not isinstance(channel_ids, list):
            return jsonify({"ok": False, "error": "channel_ids must be a list"}), 400
        try:
            dwell = float(body.get("identify_dwell_s", 2.0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "identify_dwell_s must be a number"}), 400
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        ctx.sse.ensure_transport_hooked(ctx.rl_instance)

        def _progress(payload):
            ctx.tasks.update(meta=dict(payload))

        def _runner():
            return scan.scan_region(
                gateway_id=ident_mac,
                region=region,
                channel_ids=channel_ids,
                identify_dwell_s=dwell,
                progress_cb=_progress,
            )

        task = ctx.tasks.start(
            "channel_scan", _runner,
            meta={
                "stage": "INIT",
                "ident_mac": ident_mac,
                "region": region,
                "channel_ids": channel_ids,
                "identify_dwell_s": dwell,
            },
        )
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    @bp.route("/api/gateways/<ident_mac>/resolve", methods=["POST"])
    def api_gateway_resolve(ident_mac: str):
        """Stage 3 Part D: operator response to a ``gateway_conflict``
        / ``gateway_unbound`` wizard. Body shape:

            {
                "action": "accept_gateway" | "accept_host"
                          | "create_network" | "rebind",
                "params": {...action-specific...},
            }

        ``params`` carries the ``token`` from the inbound SSE event
        so a stale wizard cannot override a re-evaluated record.
        """
        bind_service = getattr(ctx.rl_instance, "gateway_bind_service", None)
        if bind_service is None:
            return jsonify({"ok": False, "error": "bind service unavailable"}), 503
        body = request.get_json(silent=True) or {}
        action = str(body.get("action") or "").strip()
        if not action:
            return jsonify({"ok": False, "error": "action required"}), 400
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        result = bind_service.resolve(ident_mac, action, params)
        status = 200 if result.get("ok") else 400
        return jsonify(result), status

    @bp.route("/api/gateway", methods=["GET"])
    def api_gateway_status():
        return jsonify({"ok": True, "gateway": _gateway_status(ctx)})

    @bp.route("/api/health", methods=["GET"])
    def api_health():
        """Cheap liveness probe for the WebUI's auto-reconnect path.

        Kept separate from ``/api/master`` so the browser can hammer it during
        reconnect without paying for the full state roundtrip.
        """
        rl = getattr(ctx, "rl_instance", None)
        startup_done = bool(getattr(rl, "_startup_done", False)) if rl else True
        return jsonify({
            "ok": True,
            "ts": time.time(),
            "phase": "ready" if startup_done else "booting",
        })

    @bp.route("/api/gateway/retry", methods=["POST"])
    def api_gateway_retry():
        rl = ctx.rl_instance
        retry = getattr(rl, "retry_gateway", None)
        if callable(retry):
            status = retry()
        else:  # pragma: no cover - legacy host without retry helper
            status = _gateway_status(ctx)
        return jsonify({"ok": bool(status.get("ready")), "gateway": status})

    @bp.route("/api/gateway/query-state", methods=["POST"])
    def api_gateway_query_state():
        """Send GW_CMD_STATE_REQUEST and return the gateway's STATE_REPORT reply.

        Used by the master-pill ↻ refresh affordance and as a startup
        synchroniser. The request is bounded by a short timeout (~500 ms)
        so a stalled gateway doesn't block the WebUI thread; the fallback
        reports the host's last-mirrored state with ``ok=False``.
        """
        rl = ctx.rl_instance
        gw = getattr(rl, "gateway_service", None)
        query = getattr(gw, "query_state", None) if gw is not None else None
        if not callable(query):
            # Defensive: a partially-initialised host without the gateway
            # service should still fail clean rather than 500.
            return jsonify({
                "ok": False,
                "state": "UNKNOWN",
                "state_byte": 0xFF,
                "state_metadata_ms": 0,
                "error": "gateway_service unavailable",
            }), 503
        result = query()
        # The gateway driving the master state mirrors itself via the SSE
        # bridge whenever EV_STATE_REPORT lands; surfacing the snapshot
        # here is for the synchronous caller (the WebUI fetch).
        return jsonify(result)

    @bp.route("/api/gateways/query-state", methods=["POST"])
    def api_gateways_query_state():
        """Round 3 Task 4: fan out STATE_REQUEST to every attached
        transport and return one result per gateway. The new MasterBar
        ↻-button uses this so all per-gateway pills refresh at once.

        Returns ``{ok: True, gateways: [{ident_mac, state, ...}, ...]}``.
        """
        rl = ctx.rl_instance
        gw = getattr(rl, "gateway_service", None)
        query = getattr(gw, "query_state", None) if gw is not None else None
        if not callable(query):
            return jsonify({"ok": False, "error": "gateway_service unavailable"}), 503
        transports = list(getattr(rl, "transports", None) or ())
        results = []
        for t in transports:
            # Ethernet transports have no LoRa state machine to query — they
            # report a constant IDLE. Return that snapshot directly so the ↻
            # refresh doesn't reset their pill to UNKNOWN waiting for a
            # STATE_REPORT that never arrives.
            if getattr(t, "kind", "rf") == "ethernet":
                snap = (
                    t.gateway_state_snapshot()
                    if hasattr(t, "gateway_state_snapshot") else {}
                )
                r = dict(snap)
                r["ident_mac"] = str(getattr(t, "ident_mac", "") or "") or None
                results.append(r)
                continue
            r = query(transport=t)
            if isinstance(r, dict):
                r = dict(r)
                r["ident_mac"] = (getattr(t, "ident_mac", "") or "").upper() or None
            results.append(r)
        return jsonify({"ok": True, "gateways": results})

    @bp.route("/api/gateway/rediscover", methods=["POST"])
    def api_gateway_rediscover():
        """Round 3 Task 3: manual re-discover trigger. Runs
        ``soft_rediscover`` synchronously and clears the tracker's
        cancel list so a previously-cancelled MAC becomes pollable
        again. Operator entry-point lives in the Pair Assistant.
        """
        rl = ctx.rl_instance
        soft = getattr(rl, "soft_rediscover", None)
        tracker = getattr(rl, "missing_transport_tracker", None)
        if not callable(soft) or tracker is None:
            return jsonify({
                "ok": False,
                "error": "controller does not support soft rediscover",
            }), 503
        try:
            tracker.clear_cancelled()
            attached = int(soft())
        except Exception as ex:
            logger.exception("api_gateway_rediscover: soft_rediscover failed")
            return jsonify({
                "ok": False,
                "error": f"{type(ex).__name__}: {ex}",
            }), 500
        # evaluate_and_arm broadcasts the post-rediscover state.
        try:
            tracker.evaluate_and_arm()
        except Exception:
            pass  # swallow-ok: re-arm already logs internally
        return jsonify({
            "ok": True,
            "attached": attached,
            "missing": tracker.snapshot(),
        })

    @bp.route("/api/gateway/cancel-reconnect", methods=["POST"])
    def api_gateway_cancel_reconnect():
        """Round 3 Task 5: operator-driven suppression of the
        reconnect poll for a specific ident_mac (or all currently-
        missing transports when body's ``ident_mac`` is null/absent).
        Re-enable later with ``/api/gateway/rediscover``.
        """
        rl = ctx.rl_instance
        tracker = getattr(rl, "missing_transport_tracker", None)
        if tracker is None:
            return jsonify({
                "ok": False,
                "error": "missing_transport_tracker unavailable",
            }), 503
        body = request.get_json(silent=True) or {}
        ident_mac = body.get("ident_mac")
        if ident_mac is not None and not isinstance(ident_mac, str):
            return jsonify({
                "ok": False,
                "error": "ident_mac must be a string or null",
            }), 400
        try:
            tracker.cancel(ident_mac if ident_mac else None)
        except Exception as ex:
            logger.exception("api_gateway_cancel_reconnect: tracker.cancel failed")
            return jsonify({
                "ok": False,
                "error": f"{type(ex).__name__}: {ex}",
            }), 500
        return jsonify({
            "ok": True,
            "cancelled": sorted(tracker.cancelled_macs()),
            "missing": tracker.snapshot(),
        })

    @bp.route("/api/onboarding/repair", methods=["POST"])
    def api_onboarding_repair():
        """Run a single-gateway onboarding repair (Stage 1.5).

        Body shape:
            {
                "case": "A" | "B" | "C",
                "params": { ... }   # case-specific, see below
            }

        Case A params: {"target_macs": [str, ...] | null, "run_discover": bool?}
        Case B params: {
            "old_rf_config": {...},
            "new_rf_config": {...},
            "target_macs": [str, ...] | null
        }
        Case C params: {"device_rf_config": {...}}

        Long-running cases (B and C reboot the gateway) run inside a
        TaskManager job; the response carries the task handle and the
        live progress streams over the existing ``task`` SSE channel.
        Case A stays inline (typical wall-clock < 2 s per device).
        """
        rl = ctx.rl_instance
        onboarding = getattr(rl, "onboarding_service", None)
        if onboarding is None:
            return jsonify({"ok": False, "error": "onboarding_service unavailable"}), 503

        body = request.get_json(silent=True) or {}
        case = str(body.get("case", "")).strip().upper()
        params = body.get("params") or {}
        if case not in ("A", "B", "C"):
            return jsonify({"ok": False, "error": "case must be 'A', 'B', or 'C'"}), 400

        # Common per-case payload validation. Bad payloads die early so
        # we never block on a TaskManager job for a malformed request.
        if case == "B":
            for key in ("old_rf_config", "new_rf_config"):
                if not isinstance(params.get(key), dict):
                    return jsonify({"ok": False, "error": f"params.{key} dict required"}), 400
        if case == "C":
            if not isinstance(params.get("device_rf_config"), dict):
                return jsonify({"ok": False, "error": "params.device_rf_config dict required"}), 400

        # Case A: synchronous (short; no gateway reboot).
        if case == "A":
            result = onboarding.case_a_re_pair(
                target_macs=params.get("target_macs"),
                run_discover=bool(params.get("run_discover", True)),
            )
            return jsonify({"ok": result.get("ok", False), "result": result})

        # Cases B / C: wrap in a TaskManager job so the reboot wait
        # doesn't tie up the Flask thread.
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        ctx.sse.ensure_transport_hooked(rl)

        def _progress(progress):
            ctx.tasks.update(meta={**progress, "case": case})

        if case == "B":
            def _runner():
                return onboarding.case_b_migrate(
                    old_rf_config=params["old_rf_config"],
                    new_rf_config=params["new_rf_config"],
                    target_macs=params.get("target_macs"),
                    progress_cb=_progress,
                )
            task = ctx.tasks.start("onboarding_case_b", _runner, meta={"case": "B"})
        else:  # case == "C"
            def _runner():
                return onboarding.case_c_align_gateway(
                    device_rf_config=params["device_rf_config"],
                    progress_cb=_progress,
                )
            task = ctx.tasks.start("onboarding_case_c", _runner, meta={"case": "C"})

        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    @bp.route("/api/devices/<mac>/rf_config", methods=["GET"])
    def api_device_rf_config_get(mac):
        """Read back a node's currently active LoRa PHY config via OPC_GET_RF_CONFIG.

        Returns ``{"ok": bool, "rf_config": {...}}`` on success. 404 if
        the device is not in the host repo, 504 on transport timeout,
        503 if gateway_service is unavailable.
        """
        rl = ctx.rl_instance
        gw = getattr(rl, "gateway_service", None)
        query = getattr(gw, "query_node_rf_config", None) if gw is not None else None
        if not callable(query):
            return jsonify({"ok": False, "error": "gateway_service unavailable"}), 503
        mac_str = str(mac or "").strip().upper()
        if len(mac_str) != 12:
            return jsonify({"ok": False, "error": "mac must be 12 hex chars"}), 400
        with ctx.rl_lock:
            dev = rl.getDeviceFromAddress(mac_str)
        if not dev:
            return jsonify({"ok": False, "error": "device not found"}), 404
        result = query(mac_str)
        if not result.get("ok"):
            return jsonify(result), 504
        return jsonify(result)

    @bp.route("/api/devices/<mac>/rf_config", methods=["POST"])
    def api_device_rf_config_post(mac):
        """Push a new LoRa PHY config to a node via OPC_RF_CONFIG.

        Expected JSON body:
            { "rf_config": {freq_hz, bw_khz_x10, sf, cr_den, sync_word,
                            tx_power_dbm, preamble} }

        The node validates the payload, persists to NVS, ACKs, then
        reboots onto the new config. The reboot drops the link briefly;
        the success signal is the ACK.

        Returns ``{"ok": bool, "ack_status": int}``; 400 on validation
        rejection / payload errors, 504 on timeout, 503 if service
        unavailable, 404 if device unknown.
        """
        rl = ctx.rl_instance
        gw = getattr(rl, "gateway_service", None)
        setter = getattr(gw, "set_node_rf_config", None) if gw is not None else None
        if not callable(setter):
            return jsonify({"ok": False, "error": "gateway_service unavailable"}), 503

        mac_str = str(mac or "").strip().upper()
        if len(mac_str) != 12:
            return jsonify({"ok": False, "error": "mac must be 12 hex chars"}), 400

        with ctx.rl_lock:
            dev = rl.getDeviceFromAddress(mac_str)
        if not dev:
            return jsonify({"ok": False, "error": "device not found"}), 404

        payload = request.get_json(silent=True) or {}
        rf_config = payload.get("rf_config")
        if not isinstance(rf_config, dict):
            return jsonify({"ok": False, "error": "rf_config dict required"}), 400
        required = ("freq_hz", "bw_khz_x10", "sf", "cr_den", "sync_word",
                    "tx_power_dbm", "preamble")
        missing = [k for k in required if k not in rf_config]
        if missing:
            return jsonify({
                "ok": False,
                "error": f"rf_config missing fields: {missing}",
            }), 400

        result = setter(mac_str, rf_config)
        if result.get("ok"):
            return jsonify(result)
        if result.get("error") == "timeout":
            return jsonify(result), 504
        # ack_status != 0 = node-side rejection (validation / NVS). 400
        # to surface "bad parameters" semantics to the operator.
        if "ack_status" in result:
            return jsonify(result), 400
        return jsonify(result), 503

    def _resolve_transport_by_ident(ident_mac: str):
        """Walk the controller's transport list and return the one whose
        ``ident_mac`` matches. Case-insensitive. Returns ``None`` if no
        attached gateway carries that MAC."""
        rl = ctx.rl_instance
        transports = list(getattr(rl, "transports", None) or [])
        target = str(ident_mac or "").upper()
        for t in transports:
            if str(getattr(t, "ident_mac", "") or "").upper() == target:
                return t
        return None

    def _rf_config_get(transport):
        rl = ctx.rl_instance
        gw = getattr(rl, "gateway_service", None)
        query = getattr(gw, "query_gateway_rf_config", None) if gw is not None else None
        if not callable(query):
            return jsonify({"ok": False, "error": "gateway_service unavailable"}), 503
        result = query(transport=transport) if transport is not None else query()
        if not result.get("ok"):
            # 504 = gateway timeout (USB connected but no reply within bound).
            return jsonify(result), 504
        return jsonify(result)

    def _rf_config_set(transport):
        rl = ctx.rl_instance
        gw = getattr(rl, "gateway_service", None)
        setter = getattr(gw, "set_gateway_rf_config", None) if gw is not None else None
        if not callable(setter):
            return jsonify({"ok": False, "error": "gateway_service unavailable"}), 503

        payload = request.get_json(silent=True) or {}
        rf_config = payload.get("rf_config")
        if not isinstance(rf_config, dict):
            return jsonify({"ok": False, "error": "rf_config dict required"}), 400

        required = ("freq_hz", "bw_khz_x10", "sf", "cr_den", "sync_word",
                    "tx_power_dbm", "preamble")
        missing = [k for k in required if k not in rf_config]
        if missing:
            return jsonify({
                "ok": False,
                "error": f"rf_config missing fields: {missing}",
            }), 400

        persist = bool(payload.get("persist", True))
        kwargs = {"persist": persist}
        if transport is not None:
            kwargs["transport"] = transport
        result = setter(rf_config, **kwargs)
        if result.get("ok"):
            return jsonify(result)
        # reason-based status code: range / NVS rejections are 400 (caller
        # gave bad input); transport timeouts are 504.
        reason = result.get("reason")
        if reason is None:
            return jsonify(result), 504
        return jsonify(result), 400

    @bp.route("/api/gateway/rf_config", methods=["GET"])
    def api_gateway_rf_config_get():
        """Legacy single-gateway read of the LoRa PHY config — defaults
        to the controller's primary transport slot. New code should use
        ``GET /api/gateways/<ident_mac>/rf_config`` for explicit
        addressing on multi-gateway deployments.

        Sends GW_CMD_GET_RF_CONFIG; replies via EV_RF_CHANGED. Returns
        ``{"ok": bool, "rf_config": {...}}`` on success.

        Body fields (P_RfConfig wire layout):
            freq_hz, bw_khz_x10, sf, cr_den, sync_word, tx_power_dbm,
            preamble.
        """
        return _rf_config_get(transport=None)

    @bp.route("/api/gateway/rf_config", methods=["POST"])
    def api_gateway_rf_config_set():
        """Legacy single-gateway write of the LoRa PHY config — defaults
        to the controller's primary transport slot. New code should use
        ``POST /api/gateways/<ident_mac>/rf_config`` for explicit
        addressing on multi-gateway deployments.

        Expected JSON body:
            {
                "rf_config": {
                    "freq_hz": 867700000,
                    "bw_khz_x10": 1250,
                    "sf": 7, "cr_den": 5, "sync_word": 18,
                    "tx_power_dbm": 14, "preamble": 8
                },
                "persist": true  # optional, default true
            }

        ``persist=true`` writes NVS and reboots the gateway. ``persist=false``
        live-reconfigures without persisting (channel-scan mode). The reply
        echoes the EV_RF_CHANGED reason; HTTP 400 for validation rejections,
        504 for transport timeouts.
        """
        return _rf_config_set(transport=None)

    @bp.route("/api/gateways/<ident_mac>/rf_config", methods=["GET"])
    def api_gateway_rf_config_get_for(ident_mac: str):
        """Per-gateway read of the LoRa PHY config (multi-gateway).

        Same response shape as ``/api/gateway/rf_config``. Returns 404
        if no attached transport carries ``ident_mac``.
        """
        transport = _resolve_transport_by_ident(ident_mac)
        if transport is None:
            return jsonify({
                "ok": False,
                "error": f"no attached gateway with ident_mac={ident_mac!r}",
            }), 404
        return _rf_config_get(transport=transport)

    @bp.route("/api/gateways/<ident_mac>/rf_config", methods=["POST"])
    def api_gateway_rf_config_set_for(ident_mac: str):
        """Per-gateway write of the LoRa PHY config (multi-gateway).

        Same request/response shape as ``/api/gateway/rf_config``.
        Returns 404 if no attached transport carries ``ident_mac``.
        """
        transport = _resolve_transport_by_ident(ident_mac)
        if transport is None:
            return jsonify({
                "ok": False,
                "error": f"no attached gateway with ident_mac={ident_mac!r}",
            }), 404
        return _rf_config_set(transport=transport)

    @bp.route("/api/task", methods=["GET"])
    def api_task():
        return jsonify({"ok": True, "task": ctx.tasks.snapshot()})

    @bp.route("/api/task/cancel", methods=["POST"])
    def api_task_cancel():
        """Cooperative cancel for the currently running long task.

        Sets the task's cancel flag and returns immediately. The worker
        polls :meth:`TaskManager.is_cancel_requested` at its own cancel
        points and winds down — for OTA that means "skip remaining
        devices after the current one completes". The dialog stays open
        through the WebUI lockdown until the resulting summary lands.
        """
        signalled = ctx.tasks.request_cancel()
        if not signalled:
            return jsonify({"ok": False, "error": "no task running"}), 200
        return jsonify({"ok": True, "task": ctx.tasks.snapshot()})

    @bp.route("/api/options", methods=["GET"])
    def api_options():
        return jsonify({"ok": True, "presets": wled_preset_select_options(context={"rl_instance": ctx.rl_instance})})

    @bp.route("/api/discover", methods=["POST"])
    def api_discover():
        ctx.sse.ensure_transport_hooked(ctx.rl_instance)
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        target_gid, created_gid = _prepare_discover_target(
            ctx,
            target_gid=body.get("targetGroupId", None),
            new_group_name=body.get("newGroupName", None),
        )

        # ``discoveryGroup`` is the *filter* used in the OPC_DEVICES wire
        # body — independent of ``targetGroupId`` which is the
        # add-discovered-devices-to group. See the broadcast ruleset for
        # why discovery defaults to groupId=0 (newly-booted devices) and
        # why "all groups" is a sweep, not a single packet:
        # ../../docs/reference/broadcast-ruleset.md#designed-in-special-cases
        # ../../docs/roadmap.md#group-agnostic-re-identification
        raw_dgroup = body.get("discoveryGroup", None)
        sweep_all = (str(raw_dgroup).lower() == "all") if raw_dgroup is not None else False
        if sweep_all:
            try:
                known_groups = ctx.rl_instance.group_repository.list()
                sweep_ids = sorted({
                    int(getattr(g, "groupId", getattr(g, "id", -1)))
                    for g in known_groups
                    if 0 <= int(getattr(g, "groupId", getattr(g, "id", -1))) <= 254
                })
            except Exception:
                # swallow-ok: missing/empty repo → degrade to default
                # filter (0); the operator sees "0 found" in that case.
                sweep_ids = []
            discovery_filter = None  # signal: use the sweep path
        else:
            try:
                discovery_filter = (
                    int(raw_dgroup) if raw_dgroup not in (None, "") else 0
                )
            except (TypeError, ValueError):
                discovery_filter = 0
            if not 0 <= discovery_filter <= 254:
                discovery_filter = 0
            sweep_ids = []

        def do_discover():
            add_to_group = -1
            if target_gid not in (None, 0, "0"):
                add_to_group = int(target_gid)
            if sweep_all and sweep_ids:
                found = int(
                    ctx.rl_instance.getDevicesInGroups(
                        groupIds=sweep_ids, addToGroup=add_to_group,
                    ) or 0
                )
            else:
                # Default + per-group path: single OPC_DEVICES with the
                # selected filter. ``discoveryGroup`` missing or invalid
                # falls back to 0 (Unconfigured) — the historical default.
                found = int(
                    ctx.rl_instance.getDevices(
                        groupFilter=(discovery_filter if discovery_filter is not None else 0),
                        addToGroup=add_to_group,
                    ) or 0
                )
            return {
                "found": found,
                "createdGroupId": created_gid,
                "targetGroupId": target_gid,
                "discoveryGroup": "all" if sweep_all else discovery_filter,
            }

        task = ctx.tasks.start(
            "discover", do_discover,
            meta={
                "createdGroupId": created_gid,
                "targetGroupId": target_gid,
                "discoveryGroup": "all" if sweep_all else discovery_filter,
            },
        )
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    @bp.route("/api/status", methods=["POST"])
    def api_status():
        ctx.sse.ensure_transport_hooked(ctx.rl_instance)
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        selection = body.get("selection") or body.get("macs") or []
        group_id = body.get("groupId", None)

        def do_status():
            updated = 0
            retried = 0
            retried_success = 0
            status_service = getattr(ctx.rl_instance, "status_service", None)
            if selection:
                # Selection-based "Get Status" is already a per-device
                # series of unicasts — no retry pass needed (each
                # device gets its full idle-timeout window already).
                for mac in selection:
                    dev = ctx.rl_instance.getDeviceFromAddress(mac)
                    if dev:
                        updated += int(ctx.rl_instance.getStatus(targetDevice=dev) or 0)
            else:
                group_filter = int(group_id) if group_id is not None else 255
                if status_service is not None:
                    result = status_service.get_status(group_filter=group_filter) or {}
                    updated = int(result.get("updated") or 0)
                    retried = int(result.get("retried") or 0)
                    rr = result.get("retried_responders")
                    if isinstance(rr, set):
                        retried_success = len(rr)
                else:
                    updated = int(ctx.rl_instance.getStatus(groupFilter=group_filter) or 0)
            return {
                "updated": updated,
                "retried": retried,
                "retried_success": retried_success,
                "groupId": group_id,
                "selectionCount": len(selection) if selection else 0,
            }

        task = ctx.tasks.start("status", do_status, meta={"groupId": group_id, "selectionCount": len(selection) if selection else 0})
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    @bp.route("/api/host-settings", methods=["GET"])
    def api_host_settings_get():
        if host_settings_service is None:
            return jsonify({"ok": False, "error": "host-settings service unavailable"}), 500
        return jsonify({
            "ok": True,
            "battery": host_settings_service.get_battery_thresholds(),
        })

    @bp.route("/api/host-settings", methods=["POST"])
    def api_host_settings_post():
        if host_settings_service is None:
            return jsonify({"ok": False, "error": "host-settings service unavailable"}), 500
        body = request.get_json(silent=True) or {}
        battery = body.get("battery") or {}
        try:
            updated = host_settings_service.set_battery_thresholds(
                mv_2s=battery.get("mV_2s"),
                mv_6s=battery.get("mV_6s"),
            )
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        # The DTO bakes ``battery_low`` from the live threshold, so a
        # change requires the frontend to re-fetch /api/devices. Reuse
        # the existing ``devices`` SSE topic instead of inventing a new
        # one — the banner reads ``battery_low`` from devices.
        _sse_refresh(ctx, {state_scope.DEVICES})
        return jsonify({"ok": True, "battery": updated})

    @bp.route("/api/devices/update-meta", methods=["POST"])
    def api_devices_update_meta():
        """Bulk rename / regroup for selected devices.

        Pre-2026-04-29 this was synchronous: the route blocked until
        every per-device ``setNodeGroupId`` completed (8 s timeout
        per offline device → minutes of frozen UI for a fleet with
        offline nodes). Now:

        * Pure-rename requests (no ``groupId`` field) stay
          synchronous — they're in-memory mutations only, no RF I/O,
          so a fast response is the right shape.
        * Group-change requests run inside a TaskManager job. The
          route returns immediately with the task handle; the
          frontend's ``updateTask`` shows per-device progress in the
          masterbar. The runner skips the wire send for already-
          offline devices (``_apply_device_meta_updates`` enforces
          this) so the wait time is bounded by the number of online
          devices that need ACKs, not the total fleet size.
        """
        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        new_group = body.get("groupId", None)
        new_name = body.get("name", None)
        raw_names = body.get("names")
        names = raw_names if isinstance(raw_names, dict) else None

        # Pure rename: keep the synchronous path. No RF I/O, no need
        # for the TaskManager wrapper.
        if new_group is None:
            result = _apply_device_meta_updates(
                ctx, macs=macs, new_group=None, new_name=new_name, names=names,
            )
            renamed = new_name is not None or names is not None
            scopes = {state_scope.DEVICES} if renamed else {state_scope.NONE}
            try:
                ctx.rl_instance.save_to_db({"manual": True}, scopes=scopes)
            except Exception as ex:
                ctx.log(
                    f"RaceLink: save_to_db after update-meta failed: "
                    f"{type(ex).__name__}: {ex}"
                )
                logger.warning("save_to_db after update-meta failed", exc_info=True)
            _sse_refresh(ctx, scopes)
            return jsonify({"ok": True, **result})

        # Stage 3 Part B: hard-enforce the network boundary before
        # we kick off the task. Catches both "selected devices span
        # multiple networks" and "target group is on a different
        # network". Moving to Unconfigured (id 0) short-circuits the
        # check inside the validator. The check runs under the same
        # ``rl_lock`` shape the runner uses for in-memory mutations
        # so a concurrent device delete cannot race us into a stale
        # decision.
        with ctx.rl_lock:
            try:
                target_group_id = int(new_group)
            except (TypeError, ValueError):
                target_group_id = -1
            groups_list = ctx.groups()
            target_group = (
                groups_list[target_group_id]
                if 0 <= target_group_id < len(groups_list)
                else None
            )
            devices_for_check = [
                ctx.rl_instance.getDeviceFromAddress(m) for m in macs
            ]
            devices_for_check = [d for d in devices_for_check if d is not None]
        try:
            validate_group_membership(
                devices_for_check,
                target_group,
                target_group_id=target_group_id,
            )
        except NetworkBoundaryViolation as ex:
            return jsonify({
                "ok": False,
                "error": ex.reason,
                "detail": ex.detail,
            }), 400

        # Group change: wrap in a TaskManager job for live progress.
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        ctx.sse.ensure_transport_hooked(ctx.rl_instance)

        scopes_set = {state_scope.DEVICE_MEMBERSHIP}
        if new_name is not None or names is not None:
            scopes_set.add(state_scope.DEVICES)
        target_group = int(new_group)

        def _progress(index, total, mac, stage, message):
            # Updates the task meta; the existing SSE ``task`` channel
            # delivers it to the frontend's ``updateTask`` handler.
            ctx.tasks.update(meta={
                "stage": stage, "index": index, "total": total,
                "addr": mac, "groupId": target_group,
                "message": message,
            })

        def _runner():
            outcome = _apply_device_meta_updates(
                ctx, macs=macs, new_group=new_group, new_name=new_name,
                names=names, progress_cb=_progress,
            )
            try:
                ctx.rl_instance.save_to_db({"manual": True}, scopes=scopes_set)
            except Exception as ex:
                ctx.log(
                    f"RaceLink: save_to_db after update-meta failed: "
                    f"{type(ex).__name__}: {ex}"
                )
                logger.warning("save_to_db after update-meta failed", exc_info=True)
            _sse_refresh(ctx, scopes_set)
            return outcome

        n = len(macs)
        task = ctx.tasks.start(
            "bulk_set_group", _runner,
            meta={
                "stage": "INIT",
                "index": 0,
                "total": n,
                "addr": None,
                "groupId": target_group,
                "message": f"Moving {n} device{'s' if n != 1 else ''} → group {target_group}…",
            },
        )
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    def _save_groups_quietly(what: str) -> None:
        """Persist groups state, logging traceback on failure.

        Pre-sweep this was three identical ``try: save_to_db; except
        Exception: pass`` blocks across create/rename/delete. The fix
        unifies them and replaces the silent ``pass`` with a logger
        warning carrying the exception type and traceback — a disk-
        full / permissions / DB-locked failure now leaves a trail.
        """
        try:
            ctx.rl_instance.save_to_db(
                {"manual": True}, scopes={state_scope.GROUPS}
            )
        except Exception:
            logger.warning(
                "save_to_db after groups.%s failed", what, exc_info=True,
            )

    @bp.route("/api/groups/create", methods=["POST"])
    def api_groups_create():
        body = request.get_json(silent=True) or {}
        name = str(body.get("name", "")).strip()
        dev_type = int(body.get("dev_type", body.get("device_type", 0)) or 0)
        if not name:
            return jsonify({"ok": False, "error": "name required"}), 400
        # A new group is network-agnostic: it carries no RF/Ethernet
        # binding (``network_id=None``) until a device joins it. The first
        # member decides the group's network — and therefore its kind —
        # via ``reconcile_group_network`` on the move; an emptied group
        # reverts to ``None``. The boundary validator already treats a
        # ``None``-network group as "no constraint, always allowed", so a
        # device from any network (RF or Ethernet) can be the first to land
        # here.
        with ctx.rl_lock:
            new_group = ctx.RL_DeviceGroup(
                name, static_group=0, dev_type=dev_type,
                network_id=None,
            )
            if ctx.group_repo is not None:
                gid = ctx.group_repo.append(new_group)
            else:
                ctx.rl_grouplist.append(new_group)
                gid = len(ctx.rl_grouplist) - 1
            _save_groups_quietly("create")
        _sse_refresh(ctx, {state_scope.GROUPS})
        return jsonify({"ok": True, "id": gid})

    @bp.route("/api/groups/rename", methods=["POST"])
    def api_groups_rename():
        body = request.get_json(silent=True) or {}
        # B1: was ``int(body.get("id"))`` which crashes the route with a
        # 500 on missing or null id; require_int returns a clean 400
        # validation error instead.
        try:
            gid = require_int(body, "id", label="group id")
        except RequestParseError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        name = str(body.get("name", "")).strip()
        with ctx.rl_lock:
            if gid < 0 or gid >= len(ctx.groups()):
                return jsonify({"ok": False, "error": "invalid group id"}), 400
            group = ctx.groups()[gid]
            if getattr(group, "static_group", 0):
                return jsonify({"ok": False, "error": "static group"}), 400
            group.name = name or group.name
            _save_groups_quietly("rename")
        _sse_refresh(ctx, {state_scope.GROUPS})
        return jsonify({"ok": True})

    @bp.route("/api/groups/resort", methods=["POST"])
    def api_groups_resort():
        """Re-order user groups.

        Body: ``{order: [<group_id>, ...], carry_scene_references: bool}``.
        The ``order`` list contains every current group id exactly once,
        in the desired new sequence. Group 0 (Unconfigured) and any
        static group keep their existing index — the dialog enforces
        this client-side and the route rejects any payload that moves
        them.

        Behaviour:
        * The group repository is reordered.
        * Every device's ``groupId`` is rewritten through the
          ``{old_gid: new_gid}`` mapping so the host's view of "which
          group is each device in" tracks the new ids.
        * When ``carry_scene_references`` is true (default), the same
          mapping is applied to every scene's group references via
          :meth:`SceneService.remap_group_ids` — operator intent
          ("scene targets the same physical group") is preserved.
        * When false, scene group ids stay numerically frozen, which
          means they now target whatever group ended up in those
          slots. Operator-footgun by design.
        """
        body = request.get_json(silent=True) or {}
        raw_order = body.get("order")
        carry_refs = bool(body.get("carry_scene_references", True))

        if not isinstance(raw_order, list):
            return jsonify({
                "ok": False, "error": "order must be a list of group ids",
            }), 400
        try:
            new_order = [int(g) for g in raw_order]
        except (TypeError, ValueError):
            return jsonify({
                "ok": False, "error": "order entries must be integers",
            }), 400

        scenes_changed = 0
        mapping: dict[int, int] = {}
        with ctx.rl_lock:
            groups_list = ctx.groups()
            current_ids = list(range(len(groups_list)))
            if sorted(new_order) != current_ids:
                return jsonify({
                    "ok": False,
                    "error": "order must be a permutation of all current group ids",
                }), 400

            # Group 0 (Unconfigured) is the anchor; any static group
            # must keep its index too.
            for new_idx, old_gid in enumerate(new_order):
                old_group = groups_list[old_gid]
                if old_gid == 0 and new_idx != 0:
                    return jsonify({
                        "ok": False,
                        "error": "group 0 (Unconfigured) must stay at the top",
                    }), 400
                if getattr(old_group, "static_group", 0) and new_idx != old_gid:
                    return jsonify({
                        "ok": False,
                        "error": f"static group {old_gid} cannot be moved",
                    }), 400

            mapping = {old_gid: new_idx for new_idx, old_gid in enumerate(new_order)}
            if all(old == new for old, new in mapping.items()):
                # Identity permutation — nothing to do. Return success
                # without re-broadcasting SSE so the operator-facing
                # event stream stays calm.
                return jsonify({"ok": True, "scenes_changed": 0, "mapping": {}})

            new_groups = [groups_list[old_gid] for old_gid in new_order]
            if ctx.group_repo is not None:
                ctx.group_repo.replace_all(new_groups)
            else:
                ctx.rl_grouplist[:] = new_groups

            for device in ctx.devices():
                try:
                    cur = int(getattr(device, "groupId", 0) or 0)
                except (TypeError, ValueError):
                    continue
                new_gid = mapping.get(cur, cur)
                if new_gid != cur:
                    device.groupId = new_gid

            if carry_refs and scenes_service is not None:
                try:
                    scenes_changed = scenes_service.remap_group_ids(mapping)
                except Exception:
                    # swallow-ok: groups + devices are the critical
                    # path; a failed scene rewrite leaves stale refs
                    # but doesn't block the resort. Logged for diag.
                    logger.warning(
                        "remap_group_ids failed during group resort",
                        exc_info=True,
                    )

            _save_groups_quietly("resort")

        scopes = {state_scope.GROUPS, state_scope.DEVICE_MEMBERSHIP}
        if scenes_changed > 0:
            scopes.add(state_scope.SCENES)
        _sse_refresh(ctx, scopes)

        # JSON object keys must be strings; only emit pairs that
        # actually moved so the client can render a concise summary.
        moved = {str(old): new for old, new in mapping.items() if old != new}
        return jsonify({
            "ok": True,
            "scenes_changed": scenes_changed,
            "mapping": moved,
        })

    @bp.route("/api/groups/delete", methods=["POST"])
    def api_groups_delete():
        """Delete a user group.

        Devices in the deleted group move to ``groupId = 0``
        (Unconfigured). Devices in higher-indexed groups have their
        ``groupId`` decremented by one so the index→group mapping
        stays valid after the array shift. Scene actions referencing
        the deleted group are rewritten via
        :meth:`SceneService.renumber_group_references`. Static groups
        (currently only "All WLED Nodes") remain undeletable.

        The auto-restore mechanism on the next status reply pushes
        the new groupIds out to firmware via SET_GROUP — operators
        don't need to take any further action.
        """
        body = request.get_json(silent=True) or {}
        try:
            gid = require_int(body, "id", label="group id")
        except RequestParseError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400

        moved_devices = 0
        renumbered_devices = 0
        renumbered_scenes = 0
        with ctx.rl_lock:
            if gid < 0 or gid >= len(ctx.groups()):
                return jsonify({"ok": False, "error": "invalid group id"}), 400
            group = ctx.groups()[gid]
            if getattr(group, "static_group", 0):
                return jsonify({"ok": False, "error": "static group"}), 400

            # Move devices in the deleted group to Unconfigured (0);
            # decrement higher-indexed devices so their groupId stays
            # consistent after the array shift below.
            for device in ctx.devices():
                try:
                    cur = int(getattr(device, "groupId", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if cur == gid:
                    device.groupId = 0
                    moved_devices += 1
                elif cur > gid:
                    device.groupId = cur - 1
                    renumbered_devices += 1

            # Rewrite scene group references the same way.
            if scenes_service is not None:
                try:
                    renumbered_scenes = scenes_service.renumber_group_references(gid)
                except Exception:
                    # swallow-ok: the group + device renumber is the
                    # critical path; a failed scene rewrite leaves
                    # stale references but doesn't block deletion.
                    # The cap-filter UI will show the stale ids on
                    # next edit. Log for diagnosis.
                    logger.warning(
                        "renumber_group_references failed during group delete",
                        exc_info=True,
                    )

            # Now actually drop the group entry.
            if ctx.group_repo is not None:
                ctx.group_repo.remove(gid)
            else:
                del ctx.rl_grouplist[gid]
            _save_groups_quietly("delete")

        # SSE: groups + devices + (if scenes were touched) scenes.
        scopes = {state_scope.GROUPS, state_scope.DEVICE_MEMBERSHIP}
        if renumbered_scenes:
            scopes.add(state_scope.SCENES)
        _sse_refresh(ctx, scopes)
        return jsonify({
            "ok": True,
            "moved_devices": moved_devices,
            "renumbered_devices": renumbered_devices,
            "renumbered_scenes": renumbered_scenes,
        })

    @bp.route("/api/groups/force", methods=["POST"])
    def api_groups_force():
        """Re-sync every device's stored groupId to the network.

        2026-04-29: rewritten to mirror ``api_devices_update_meta``'s
        TaskManager + skip-offline shape. Was synchronous + blocking
        + sent SET_GROUP to every device including offline ones,
        producing an 8 s × N_offline UI freeze with no operator
        feedback. Now:

        * Wrapped in a TaskManager job so the route returns
          immediately with the task handle; the frontend's
          ``updateTask`` shows per-device progress in the masterbar.
        * Per-attempt timeout dropped from 8 s to 1.5 s × 3 attempts
          (see :mod:`rf_timing`); transient packet loss now retries
          rather than timing out.
        * ``skip_offline`` is **optional** — body field
          ``skipOffline`` (boolean, default ``False``). The default
          is to push SET_GROUP to every device including offline
          ones (matches the operator's "re-sync ALL" mental model);
          the WebUI dialog exposes a checkbox so the operator can
          opt into the fast skip-offline path when they don't need
          the offline devices reached now.
        """
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        ctx.sse.ensure_transport_hooked(ctx.rl_instance)

        body = request.get_json(silent=True) or {}
        skip_offline = bool(body.get("skipOffline", False))

        def _progress(index, total, mac, stage, message):
            ctx.tasks.update(meta={
                "stage": stage, "index": index, "total": total,
                "addr": mac, "message": message,
            })

        scopes_set = {state_scope.DEVICE_MEMBERSHIP}

        def _runner():
            outcome = _iterate_force_groups(
                ctx, sanity_check=True,
                skip_offline=skip_offline,
                progress_cb=_progress,
            )
            _sse_refresh(ctx, scopes_set)
            return outcome

        with ctx.rl_lock:
            n = len(list(ctx.rl_instance.device_repository.list()))
        mode_hint = " (skipping offline)" if skip_offline else ""
        task = ctx.tasks.start(
            "force_groups", _runner,
            meta={
                "stage": "INIT",
                "index": 0,
                "total": n,
                "addr": None,
                "skipOffline": skip_offline,
                "message": f"Re-syncing {n} device{'s' if n != 1 else ''}{mode_hint}…",
            },
        )
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    @bp.route("/api/save", methods=["POST"])
    def api_save():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        try:
            ctx.rl_instance.save_to_db(
                {"manual": True}, scopes={state_scope.NONE}
            )
        except Exception as ex:
            # surface-as-500: persistence failure on the manual-save
            # path is critical. Log the type + traceback so the cause
            # (disk full, lock timeout, etc.) is visible.
            logger.warning("manual save_to_db failed", exc_info=True)
            return jsonify({
                "ok": False, "error": f"{type(ex).__name__}: {ex}",
            }), 500
        return jsonify({"ok": True})

    @bp.route("/api/reload", methods=["POST"])
    def api_reload():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        try:
            ctx.rl_instance.load_from_db()
        except Exception as ex:
            # surface-as-500: reload failure is critical (DB corrupt,
            # schema mismatch, disk read error). Log the full
            # traceback; surface type+message in the response so the
            # operator-facing toast is informative.
            logger.warning("load_from_db failed", exc_info=True)
            return jsonify({
                "ok": False, "error": f"{type(ex).__name__}: {ex}",
            }), 500
        _sse_refresh(ctx, {state_scope.FULL})
        return jsonify({"ok": True})

    @bp.route("/api/node-config/schema", methods=["GET"])
    def api_node_config_schema():
        """Operator-facing CONFIG-packet catalogue + per-bit ``configByte``
        labels. The WebUI reads this once at boot to populate the Node
        Config dropdown and the device-table Config-column tooltips.
        Source of truth is :mod:`racelink.domain.node_config`.
        """
        return jsonify({"ok": True, "schema": serialize_node_config_schema()})

    @bp.route("/api/config", methods=["POST"])
    def api_config():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        mac = body.get("mac", None)
        if mac and not macs:
            macs = [mac]
        if len(macs) != 1:
            return jsonify({"ok": False, "error": "select exactly one device"}), 400

        recv3 = parse_recv3_from_addr(macs[0])
        if not recv3:
            return jsonify({"ok": False, "error": "invalid mac/address"}), 400
        if recv3 == b"\xFF\xFF\xFF":
            return jsonify({"ok": False, "error": "broadcast not allowed for config"}), 400

        try:
            option = int(body.get("option", 0)) & 0xFF
            data0 = int(body.get("data0", body.get("flags", 0))) & 0xFF
            data1 = int(body.get("data1", 0)) & 0xFF
            data2 = int(body.get("data2", 0)) & 0xFF
            data3 = int(body.get("data3", 0)) & 0xFF
        except (TypeError, ValueError):
            # surface-as-400: int() on a malformed body field. Narrow
            # the catch so a real bug elsewhere in this block (a
            # KeyError, an AttributeError) bubbles up as a 500 instead
            # of being silently translated to "invalid option/data".
            return jsonify({"ok": False, "error": "invalid option/data"}), 400

        if option not in {0x01, 0x03, 0x04, 0x80, 0x81}:
            return jsonify({"ok": False, "error": "unknown config option"}), 400

        try:
            if hasattr(ctx.rl_instance, "sendConfig"):
                ctx.rl_instance.sendConfig(option=option, data0=data0, data1=data1, data2=data2, data3=data3, recv3=recv3)
            else:
                ctx.rl_instance.transport.send_config(recv3=recv3, option=option, data0=data0, data1=data1, data2=data2, data3=data3)
        except Exception as ex:
            # log-and-translate: include the type so e.g. AttributeError
            # (renamed method, like the historical sendGroupControl
            # ghost) is distinguishable from SerialException (USB
            # hiccup) in the operator-facing error.
            ctx.log(f"RaceLink: config failed: {type(ex).__name__}: {ex}")
            logger.warning("config send failed", exc_info=True)
            return jsonify({
                "ok": False, "error": f"{type(ex).__name__}: {ex}",
            }), 500

        # Diagnostic only — the gateway drives the actual state via
        # EV_STATE_CHANGED. Pre-Batch-B we set state="TX" + tx_pending=True
        # here as the host's guess at "we just wrote, must be transmitting";
        # the gateway-mirrored state byte arrives shortly anyway.
        ctx.sse.master.set(last_event="CONFIG_SENT")
        return jsonify({"ok": True, "sent": 1, "recv3": recv3.hex().upper(), "option": option, "data0": data0, "data1": data1, "data2": data2, "data3": data3})

    @bp.route("/api/specials/config", methods=["POST"])
    def api_specials_config():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        ok, payload, status = _resolve_special_config_request(ctx, body, specials_service)
        if not ok:
            return jsonify(payload), status

        mac_str = payload["mac_str"]
        key = payload["key"]
        recv3 = payload["recv3"]
        option = payload["option"]
        option_info = payload["option_info"]
        value = payload["value"]
        d0 = payload["data0"]
        d1 = payload["data1"]
        d2 = payload["data2"]
        d3 = payload["data3"]

        ctx.sse.ensure_transport_hooked(ctx.rl_instance)

        def do_special_config():
            ctx.tasks.update(meta={"mac": mac_str, "key": key, "message": f"Sending {key} (0x{int(option):02X})"})
            ok = ctx.rl_instance.sendConfig(
                option=int(option) & 0xFF,
                data0=d0,
                data1=d1,
                data2=d2,
                data3=d3,
                recv3=recv3,
                wait_for_ack=True,
                timeout_s=6.0,
            )
            if not ok:
                raise RuntimeError(f"ACK timeout for option 0x{int(option):02X}")
            with ctx.rl_lock:
                dev2 = ctx.rl_instance.getDeviceFromAddress(mac_str)
                if not dev2:
                    raise RuntimeError("device not found")
                specials_service.write_specials(dev2, option_info, value)
                try:
                    ctx.rl_instance.save_to_db(
                        {"manual": True}, scopes={state_scope.DEVICE_SPECIALS}
                    )
                except Exception:
                    # swallow-ok: in-memory specials update already
                    # happened; SSE refresh still notifies the UI.
                    # Persistence failure is logged with traceback so
                    # a recurring DB problem doesn't stay invisible.
                    logger.warning(
                        "save_to_db after specials update failed",
                        exc_info=True,
                    )
            _sse_refresh(ctx, {state_scope.DEVICE_SPECIALS})
            return {"mac": mac_str, "key": key, "value": value}

        task = ctx.tasks.start("special_config", do_special_config, meta={"mac": mac_str, "key": key, "message": "Preparing special config"})
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    @bp.route("/api/specials/config/import", methods=["POST"])
    def api_specials_config_import():
        """Adopt the device-reported value into the host-side ``dev.specials``
        without sending an ``OPC_CONFIG`` packet.

        Used by the dialog's "Import device" button after a divergence
        between the host-stored value and the live read-back. Body:
        ``{mac, key, value}`` where ``value`` matches the option's
        scalar / pair shape — the same shape ``/api/specials/get``
        returns.
        """
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        mac = body.get("mac", None)
        key = body.get("key", None)
        value = body.get("value", None)
        if not mac or not key or value is None:
            return jsonify({"ok": False, "error": "missing mac/key/value"}), 400

        mac_str = str(mac).upper()
        with ctx.rl_lock:
            dev = ctx.rl_instance.getDeviceFromAddress(mac_str)
            if not dev:
                return jsonify({"ok": False, "error": "device not found"}), 404
            option_info = specials_service.resolve_option(dev, key)
            if not option_info:
                return jsonify({"ok": False, "error": "option not supported for device"}), 400
            try:
                specials_service.validate_option_value(option_info, value)
            except ValueError as ex:
                return jsonify({"ok": False, "error": str(ex)}), 400
            written = specials_service.write_specials(dev, option_info, value)
            try:
                ctx.rl_instance.save_to_db(
                    {"manual": True}, scopes={state_scope.DEVICE_SPECIALS}
                )
            except Exception:
                logger.warning(
                    "save_to_db after specials import failed",
                    exc_info=True,
                )

        _sse_refresh(ctx, {state_scope.DEVICE_SPECIALS})
        return jsonify({"ok": True, "mac": mac_str, "key": key, "value": value, "written": written})

    @bp.route("/api/specials/action", methods=["POST"])
    def api_specials_action():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        mac = body.get("mac", None)
        fn_key = body.get("function", None) or body.get("fn", None)
        params = body.get("params", None) or {}
        if not mac or not fn_key:
            return jsonify({"ok": False, "error": "missing mac/function"}), 400

        recv3 = parse_recv3_from_addr(mac)
        if not recv3:
            return jsonify({"ok": False, "error": "invalid mac/address"}), 400
        if recv3 == b"\xFF\xFF\xFF":
            return jsonify({"ok": False, "error": "broadcast not allowed for action"}), 400

        mac_str = str(mac).upper()
        with ctx.rl_lock:
            dev = ctx.rl_instance.getDeviceFromAddress(mac_str)
            if not dev:
                return jsonify({"ok": False, "error": "device not found"}), 404
            fn_info, options_by_key = specials_service.resolve_action(dev, fn_key)

        if not fn_info:
            return jsonify({"ok": False, "error": "function not supported for device"}), 400
        if not fn_info.get("unicast", False):
            return jsonify({"ok": False, "error": "function does not support unicast"}), 400

        comm_name = fn_info.get("comm")
        if not comm_name:
            return jsonify({"ok": False, "error": "missing comm handler"}), 400
        comm_fn = getattr(ctx.rl_instance, comm_name, None)
        if not callable(comm_fn):
            return jsonify({"ok": False, "error": "comm handler not found"}), 400

        try:
            params_coerced = specials_service.coerce_action_params(fn_info, options_by_key, params)
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400

        ctx.sse.ensure_transport_hooked(ctx.rl_instance)
        with ctx.rl_lock:
            dev = ctx.rl_instance.getDeviceFromAddress(mac_str)
        if not dev:
            return jsonify({"ok": False, "error": "device not found"}), 404

        result = comm_fn(targetDevice=dev, targetGroup=None, params=params_coerced)
        if result is False:
            return jsonify({"ok": False, "error": "action failed"}), 500

        # Diagnostic only — gateway-driven state mirror updates via
        # EV_STATE_CHANGED (Batch B; see MasterState.apply_gateway_state).
        ctx.sse.master.set(last_event="SPECIAL_SENT")
        # Some actions mutate ``dev.specials`` host-side
        # (sendStartblockConfig, sendWledResetOverrides). Fire the SSE
        # refresh so the dialog rows re-bind to the new dev snapshot;
        # actions that don't mutate state still benefit (the refresh
        # is one /api/devices fetch — cheap).
        _sse_refresh(ctx, {state_scope.DEVICE_SPECIALS})
        return jsonify({"ok": True, "result": result, "function": fn_key, "params": params_coerced})

    @bp.route("/api/specials/get", methods=["POST"])
    def api_specials_get():
        """Read one option's current device-side value.

        Body: ``{mac, key}``. Sends ``OPC_GET_CONFIG`` to the device,
        waits for the ``GET_CONFIG_REPLY`` (single round-trip,
        ~600 ms with retries), and returns the unpacked value
        matching the option's declared shape (scalar / uint16-pair).

        Bypasses ``ctx.tasks.is_running()`` — the dialog opens reads
        for several options sequentially during normal operation, so
        gating on the global task lock would block dialog renders.
        Wire serialisation is provided by the gateway transport.
        """
        body = request.get_json(silent=True) or {}
        mac = body.get("mac", None)
        key = body.get("key", None)
        if not mac or not key:
            return jsonify({"ok": False, "error": "missing mac/key"}), 400

        recv3 = parse_recv3_from_addr(mac)
        if not recv3:
            return jsonify({"ok": False, "error": "invalid mac/address"}), 400
        if recv3 == b"\xFF\xFF\xFF":
            return jsonify({"ok": False, "error": "broadcast not allowed for read"}), 400

        mac_str = str(mac).upper()
        with ctx.rl_lock:
            dev = ctx.rl_instance.getDeviceFromAddress(mac_str)
            if not dev:
                return jsonify({"ok": False, "error": "device not found"}), 404
            option_info = specials_service.resolve_option(dev, key)
        if not option_info:
            return jsonify({"ok": False, "error": "option not supported for device"}), 400
        option = option_info.get("option")
        if option is None:
            return jsonify({"ok": False, "error": "option not readable"}), 400

        ctx.sse.ensure_transport_hooked(ctx.rl_instance)
        config_service = getattr(ctx.rl_instance, "config_service", None)
        if config_service is None or not hasattr(config_service, "read_config"):
            return jsonify({"ok": False, "error": "read_config unavailable"}), 500

        result = config_service.read_config(dev, int(option) & 0xFF)
        if result is None:
            return jsonify({"ok": False, "mac": mac_str, "key": key, "error": "timeout"})

        _opt, d0, d1, d2, d3 = result
        try:
            value = specials_service.unpack_option_value(option_info, d0, d1, d2, d3)
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        return jsonify({"ok": True, "mac": mac_str, "key": key, "value": value})

    @bp.route("/api/devices/control", methods=["POST"])
    def api_devices_control():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        group_id = body.get("groupId", None)

        def _toint(value, default=None):
            try:
                return int(value)
            except (TypeError, ValueError):
                # swallow-ok: int() coerce failure -> caller substitutes
                # default. Narrow the catch so a logic bug elsewhere
                # surfaces as a 500 instead of being silently defaulted.
                return default

        flags = _toint(body.get("flags", None), None)
        preset_id = _toint(body.get("presetId", None), None)
        brightness = _toint(body.get("brightness", None), None)
        if flags is None or preset_id is None or brightness is None:
            return jsonify({"ok": False, "error": "missing flags/presetId/brightness"}), 400

        # B2 cleanup: ``sendGroupControl`` was renamed to
        # ``sendGroupPreset`` in the Phase D rework; the old name no
        # longer exists on the controller, so the previous code path
        # always raised ``AttributeError`` and returned a confusing 500.
        # Removed the obsolete signature-compat ``except TypeError``
        # fallback at the same time. ``changed`` now reflects the
        # *actual* number of frames the transport accepted (B2): the
        # underlying send returns False when the gateway is offline, so
        # the route stops reporting ``changed: N`` for sends that
        # silently dropped on the floor.
        changed = 0
        try:
            if group_id is not None:
                try:
                    gid_int = int(group_id)
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": "groupId must be an integer"}), 400
                if ctx.rl_instance.sendGroupPreset(gid_int, flags, preset_id, brightness):
                    changed = 1
            elif macs:
                for mac in macs:
                    dev = ctx.rl_instance.getDeviceFromAddress(mac)
                    if dev:
                        if ctx.rl_instance.sendRaceLink(dev, flags, preset_id, brightness):
                            changed += 1
            else:
                return jsonify({"ok": False, "error": "missing macs or groupId"}), 400
        except Exception as ex:
            # log-and-translate-to-500. THIS is the broad except that
            # hid the renamed-method ``sendGroupControl`` AttributeError
            # for over a year (str(ex) on its own is barely useful to
            # an operator). Now we log type + traceback to the
            # diagnostic logger AND surface the type in the response so
            # similar regressions are visible immediately.
            ctx.log(f"RaceLink: control failed: {type(ex).__name__}: {ex}")
            logger.warning("devices/control failed", exc_info=True)
            return jsonify({
                "ok": False, "error": f"{type(ex).__name__}: {ex}",
            }), 500

        # Diagnostic only — gateway-driven state mirror updates via
        # EV_STATE_CHANGED (Batch B; see MasterState.apply_gateway_state).
        ctx.sse.master.set(last_event="CONTROL_SENT")
        return jsonify({"ok": True, "changed": changed})

    @bp.route("/api/devices/indicate", methods=["POST"])
    def api_devices_indicate():
        """Trigger the OPC_INDICATE overlay on one or more devices so the
        operator can visually locate them (default catalog row IDENTIFY = 4,
        magenta strobe).

        Naming: the route uses *indicate* (matching the wire opcode
        ``OPC_INDICATE``) — *identify* is reserved for the RF-discovery
        opcode ``OPC_DEVICES``. The operator-facing verb in the UI is
        "Locate".

        Body: ``{ "macs": ["AABBCC112233", ...],
                  "indicator_type": <int, default IDENTIFY=4>,
                  "duration_sec":   <int, default 5, clamped 0..255> }``.
        ``duration_sec == 0`` cancels a running indicator on the target
        device(s). Returns ``{"ok": true, "count": N}`` where ``N`` is the
        number of devices for which a frame was queued — unknown MACs are
        skipped, missing-transport returns ``count: 0`` without erroring.
        """
        body = request.get_json(silent=True) or {}
        macs = body.get("macs")
        if not isinstance(macs, list) or len(macs) == 0:
            return jsonify({"ok": False, "error": "macs must be a non-empty list"}), 400

        try:
            indicator_type = int(body.get("indicator_type", IndicatorType.IDENTIFY)) & 0xFF
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "indicator_type must be an integer"}), 400
        try:
            duration_sec = int(body.get("duration_sec", DEFAULT_INDICATE_DURATION_SEC))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "duration_sec must be an integer"}), 400
        duration_sec = max(0, min(255, duration_sec))

        count = 0
        control_service = getattr(ctx.rl_instance, "control_service", None)
        if control_service is None:
            return jsonify({"ok": False, "error": "control_service unavailable"}), 503
        for mac in macs:
            dev = ctx.rl_instance.getDeviceFromAddress(str(mac))
            if dev is None:
                continue
            if control_service.send_device_indicate(dev, indicator_type, duration_sec):
                count += 1
        return jsonify({"ok": True, "count": count})

    @bp.route("/api/fw/upload", methods=["POST"])
    def api_fw_upload():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        try:
            info = ota_service.store_upload(request.files.get("file", None), (request.form.get("kind") or "").strip().lower())
            return jsonify({"ok": True, "file": {k: info[k] for k in ("id", "kind", "name", "size", "sha256", "uploaded_ts")}})
        except Exception as ex:
            # surface-as-400: store_upload validates the file (size,
            # MIME, kind) and raises on bad input; treating those as
            # client errors is correct. Log the type so a real bug
            # (e.g. AttributeError on a moved method) is visible.
            logger.warning("fw upload rejected", exc_info=True)
            return jsonify({
                "ok": False, "error": f"{type(ex).__name__}: {ex}",
            }), 400

    @bp.route("/api/presets/upload", methods=["POST"])
    def api_presets_upload():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        file_obj = request.files.get("file", None)
        try:
            info = presets_service.store_uploaded_file(file_obj)
            _sse_refresh(ctx, {state_scope.WLED_PRESETS})
            return jsonify({"ok": True, "file": {"name": info["name"], "size": info["size"], "saved_ts": info["saved_ts"]}, "files": presets_service.list_files()})
        except Exception as ex:
            # surface-as-400: same shape as the fw upload route.
            logger.warning("presets upload rejected", exc_info=True)
            return jsonify({
                "ok": False, "error": f"{type(ex).__name__}: {ex}",
            }), 400

    @bp.route("/api/presets/list", methods=["GET"])
    def api_presets_list():
        files = presets_service.list_files()
        current = presets_service.get_current_name()
        if current and not presets_service.preset_path_for_name(current):
            current = ""
        if not current and files:
            current = files[0]["name"]
        return jsonify({"ok": True, "files": files, "current": current})

    @bp.route("/api/presets/select", methods=["POST"])
    def api_presets_select():
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        path = presets_service.preset_path_for_name(name)
        if not path:
            return jsonify({"ok": False, "error": "presets file not found"}), 404
        if not presets_service.apply_from_path(path):
            return jsonify({"ok": False, "error": "failed to parse presets.json"}), 400
        presets_service.set_current_name(name)
        _sse_refresh(ctx, {state_scope.WLED_PRESETS})
        return jsonify({"ok": True, "current": name})

    # ------------------------------------------------------------------
    # Phase B: RaceLink-native presets (OPC_CONTROL_ADV parameter snapshots)
    # ------------------------------------------------------------------

    def _rl_presets_unavailable():
        return jsonify({"ok": False, "error": "rl_presets service not available"}), 503

    @bp.route("/api/rl-presets", methods=["GET"])
    def api_rl_presets_list():
        if rl_presets_service is None:
            return _rl_presets_unavailable()
        return jsonify({"ok": True, "presets": rl_presets_service.list()})

    @bp.route("/api/rl-presets/schema", methods=["GET"])
    def api_rl_presets_schema():
        """Return the 14-field editor schema with generators resolved.

        The Specials ``rl_preset`` action only carries the
        preset-picker form; the full editor lives in ``dlgRlPresets`` and
        needs its own schema source (``RL_PRESET_EDITOR_SCHEMA``).
        """
        schema = serialize_rl_preset_editor_schema(
            context={"rl_instance": ctx.rl_instance}
        )
        return jsonify({"ok": True, "schema": schema})

    @bp.route("/api/rl-presets", methods=["POST"])
    def api_rl_presets_create():
        if rl_presets_service is None:
            return _rl_presets_unavailable()
        body = request.get_json(silent=True) or {}
        label = body.get("label")
        if not isinstance(label, str) or not label.strip():
            return jsonify({"ok": False, "error": "label is required"}), 400
        try:
            preset = rl_presets_service.create(
                label=label,
                params=body.get("params"),
                flags=body.get("flags"),
                key=body.get("key"),
            )
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        _sse_refresh(ctx, {state_scope.RL_PRESETS})
        return jsonify({"ok": True, "preset": preset})

    @bp.route("/api/rl-presets/<key>", methods=["GET"])
    def api_rl_presets_get(key):
        if rl_presets_service is None:
            return _rl_presets_unavailable()
        preset = rl_presets_service.get(key)
        if preset is None:
            return jsonify({"ok": False, "error": "preset not found"}), 404
        return jsonify({"ok": True, "preset": preset})

    @bp.route("/api/rl-presets/<key>", methods=["PUT"])
    def api_rl_presets_update(key):
        if rl_presets_service is None:
            return _rl_presets_unavailable()
        body = request.get_json(silent=True) or {}
        try:
            preset = rl_presets_service.update(
                key,
                label=body.get("label"),
                params=body.get("params"),
                flags=body.get("flags"),
            )
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        if preset is None:
            return jsonify({"ok": False, "error": "preset not found"}), 404
        _sse_refresh(ctx, {state_scope.RL_PRESETS})
        return jsonify({"ok": True, "preset": preset})

    @bp.route("/api/rl-presets/<key>", methods=["DELETE"])
    def api_rl_presets_delete(key):
        if rl_presets_service is None:
            return _rl_presets_unavailable()
        if not rl_presets_service.delete(key):
            return jsonify({"ok": False, "error": "preset not found"}), 404
        _sse_refresh(ctx, {state_scope.RL_PRESETS})
        return jsonify({"ok": True})

    @bp.route("/api/rl-presets/<key>/duplicate", methods=["POST"])
    def api_rl_presets_duplicate(key):
        if rl_presets_service is None:
            return _rl_presets_unavailable()
        body = request.get_json(silent=True) or {}
        new_label = body.get("label")
        try:
            preset = rl_presets_service.duplicate(key, new_label=new_label)
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        if preset is None:
            return jsonify({"ok": False, "error": "preset not found"}), 404
        _sse_refresh(ctx, {state_scope.RL_PRESETS})
        return jsonify({"ok": True, "preset": preset})

    # ------------------------------------------------------------------
    # Scenes — CRUD + run + editor-schema
    # ------------------------------------------------------------------

    def _scenes_unavailable():
        return jsonify({"ok": False, "error": "scenes service not available"}), 503

    def _runner_unavailable():
        return jsonify({"ok": False, "error": "scene runner not available"}), 503

    def _enforce_scene_scope(scene: dict) -> None:
        """Cross-action subset check for explicit ``network_scope``.

        Re-raises :class:`SceneScopeViolation` when the canonicalized
        scene's explicit scope references unknown networks OR an
        action's target resolves to a network outside the scope.
        Auto-mode scenes always pass. No-op (silent return) when the
        controller is not wired (e.g. unit test bypassing the route).
        """
        if ctx.rl_instance is None:
            return
        validate_scene_scope_consistency(scene, controller=ctx.rl_instance)

    @bp.route("/api/scenes", methods=["GET"])
    def api_scenes_list():
        if scenes_service is None:
            return _scenes_unavailable()
        return jsonify({"ok": True, "scenes": scenes_service.list()})

    @bp.route("/api/scenes/editor-schema", methods=["GET"])
    def api_scenes_editor_schema():
        """Return the per-kind action editor schema for the WebUI.

        Live state (preset option lists) is resolved at request time from the
        RL-preset and WLED-preset services so the editor sees current values.
        """
        sl_ctx = {"rl_instance": ctx.rl_instance}
        # Per-kind UI hints. Reuses the ``select / slider / toggle`` widget
        # vocabulary already established by RL_PRESET_EDITOR_SCHEMA.
        ui_per_kind = {
            KIND_RL_PRESET: {
                "presetId": {
                    "widget": "select",
                    "options": rl_preset_select_options(context=sl_ctx),
                },
                "brightness": {"widget": "slider", "min": 0, "max": 255},
            },
            KIND_WLED_PRESET: {
                "presetId": {
                    "widget": "select",
                    "options": wled_preset_select_options(context=sl_ctx),
                },
                "brightness": {"widget": "slider", "min": 0, "max": 255},
            },
            # ``rl_effect`` carries inline RaceLink effect parameters (no
            # preset id). The editor reuses the same 14-field schema as
            # the standalone RL-preset editor (``dlgRlPresets``) so any
            # parameter combination an operator could save as a preset
            # can also be applied one-shot from a scene.
            KIND_RL_EFFECT: serialize_rl_preset_editor_schema(
                context=sl_ctx
            )["ui"],
            KIND_STARTBLOCK: {
                "fn_key": {"widget": "select", "options": [
                    {"value": "startblock_control", "label": "Startblock Control"},
                ]},
            },
            "delay": {"duration_ms": {"widget": "slider", "min": 0, "max": 60000}},
            "sync": {},
        }
        kinds_out = []
        for entry in get_action_kinds_metadata():
            out = dict(entry)
            out["ui"] = ui_per_kind.get(entry["kind"], {})
            kinds_out.append(out)
        # Operator-facing labels for target kinds and offset-formula
        # modes (§8b). Carried alongside the wire values so the WebUI
        # can render straight from the schema rather than hard-coding
        # display strings. Container scope omits ``device`` because the
        # offset formula is per-group.
        target_kind_labels = {
            "broadcast": "Broadcast",
            "groups":    "Group",
            "device":    "Device",
        }
        target_kinds = [
            {"value": v, "label": target_kind_labels[v]}
            for v in ("broadcast", "groups", "device")
        ]
        container_target_kinds = [
            {"value": v, "label": target_kind_labels[v]}
            for v in ("broadcast", "groups")
        ]

        offset_modes = [dict(m) for m in OFFSET_FORMULA_MODE_LABELS]

        return jsonify({
            "ok": True,
            "kinds": kinds_out,
            # Same ``[{key, label}]`` shape as
            # ``/api/rl-presets/schema``'s ``flags`` field — both
            # endpoints serve from ``USER_FLAG_DEFS`` so the per-action
            # override block in the scene editor can render the same
            # human-readable labels as the RL-preset editor without a
            # client-side fallback humaniser. See
            # frontend/POST_MIGRATION_CLEANUP.md §13.
            "flags": [dict(f) for f in USER_FLAG_DEFS],
            # Unified target shape across every action — see
            # ``scenes_service._canonical_target`` and the broadcast-
            # ruleset doc. Legacy values (``scope``, singular ``group``,
            # standalone ``groups`` field on offset_group) are migrated
            # on read; they should never appear on a freshly-saved
            # scene.
            "target_kinds":             target_kinds,
            "container_target_kinds":   container_target_kinds,
            "offset_group": {
                "max_groups":   MAX_GROUPS_OFFSET_ENTRIES,
                "max_children": MAX_OFFSET_GROUP_CHILDREN,
                "group_id":     {"min": 0, "max": GROUP_ID_MAX},
                "offset_ms":    {"min": OFFSET_MS_MIN, "max": OFFSET_MS_MAX},
                "modes":        offset_modes,
                "base_ms":      {"min": -32768, "max": 32767},
                "step_ms":      {"min": -32768, "max": 32767},
                "center":       {"min": 0,      "max": GROUP_ID_MAX},
                "cycle":        {"min": 1,      "max": 255},
                # ``broadcast`` (the unified Strategy-A trigger)
                # replaces the pre-2026-05 ``groups: "all"`` checkbox.
                "supports_broadcast_target": True,
                "child_kinds":  list(OFFSET_GROUP_CHILD_KINDS),
                "child_target_kinds":      target_kinds,
            },
            # Active LoRa parameters for the cost-estimator tooltip.
            "lora": lora_parameters(),
        })

    @bp.route("/api/scenes/<key>", methods=["GET"])
    def api_scenes_get(key):
        if scenes_service is None:
            return _scenes_unavailable()
        scene = scenes_service.get(key)
        if scene is None:
            return jsonify({"ok": False, "error": "scene not found"}), 404
        return jsonify({"ok": True, "scene": scene})

    @bp.route("/api/scenes", methods=["POST"])
    def api_scenes_create():
        if scenes_service is None:
            return _scenes_unavailable()
        body = request.get_json(silent=True) or {}
        label = body.get("label")
        if not isinstance(label, str) or not label.strip():
            return jsonify({"ok": False, "error": "label is required"}), 400
        try:
            scene = scenes_service.create(
                label=label,
                actions=body.get("actions"),
                key=body.get("key"),
                stop_on_error=body.get("stop_on_error"),
                network_scope=body.get("network_scope"),
            )
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        # Cross-action scope consistency check happens AFTER service
        # canonicalization so the structural shape is already valid.
        # Repository access lives at this layer (the service is
        # repository-free for testability).
        try:
            _enforce_scene_scope(scene)
        except SceneScopeViolation as ex:
            # Roll back the just-created scene so the operator can
            # retry with corrected payload without leaving an
            # invalid record behind.
            scenes_service.delete(scene["key"])
            return jsonify({
                "ok": False,
                "error": ex.reason,
                "detail": ex.detail,
            }), 400
        _sse_refresh(ctx, {state_scope.SCENES})
        return jsonify({"ok": True, "scene": scene})

    @bp.route("/api/scenes/<key>", methods=["PUT"])
    def api_scenes_update(key):
        if scenes_service is None:
            return _scenes_unavailable()
        body = request.get_json(silent=True) or {}
        # Snapshot the pre-update scene so we can roll back if scope
        # validation rejects the post-update shape. update() returns
        # the new shape; the previous shape lives in the service cache
        # which we can re-fetch via get().
        prev_scene = scenes_service.get(key)
        try:
            scene = scenes_service.update(
                key,
                label=body.get("label"),
                actions=body.get("actions"),
                stop_on_error=body.get("stop_on_error"),
                network_scope=body.get("network_scope"),
            )
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        if scene is None:
            return jsonify({"ok": False, "error": "scene not found"}), 404
        try:
            _enforce_scene_scope(scene)
        except SceneScopeViolation as ex:
            # Restore the previous scene shape so the rejected update
            # doesn't leave a partially-applied scope on disk.
            if prev_scene is not None:
                scenes_service.update(
                    key,
                    label=prev_scene.get("label"),
                    actions=prev_scene.get("actions"),
                    stop_on_error=prev_scene.get("stop_on_error"),
                    network_scope=prev_scene.get("network_scope"),
                )
            return jsonify({
                "ok": False,
                "error": ex.reason,
                "detail": ex.detail,
            }), 400
        _sse_refresh(ctx, {state_scope.SCENES})
        return jsonify({"ok": True, "scene": scene})

    @bp.route("/api/scenes/<key>", methods=["DELETE"])
    def api_scenes_delete(key):
        if scenes_service is None:
            return _scenes_unavailable()
        if not scenes_service.delete(key):
            return jsonify({"ok": False, "error": "scene not found"}), 404
        _sse_refresh(ctx, {state_scope.SCENES})
        return jsonify({"ok": True})

    @bp.route("/api/scenes/<key>/duplicate", methods=["POST"])
    def api_scenes_duplicate(key):
        if scenes_service is None:
            return _scenes_unavailable()
        body = request.get_json(silent=True) or {}
        new_label = body.get("label")
        try:
            scene = scenes_service.duplicate(key, new_label=new_label)
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        if scene is None:
            return jsonify({"ok": False, "error": "scene not found"}), 404
        _sse_refresh(ctx, {state_scope.SCENES})
        return jsonify({"ok": True, "scene": scene})

    def _known_group_ids_from_ctx() -> list:
        """Best-effort list of currently-known group ids for the optimizer.
        Falls back to an empty list when no device repository is wired.

        ``ctx.rl_instance`` IS the controller — every other access in
        this module reads ``ctx.rl_instance.device_repository`` directly
        (e.g. line 199). Earlier code added a stray ``.controller``
        indirection here that silently returned ``None``; the resulting
        empty ``known_group_ids`` closed the optimizer's Strategy-C
        gate, making the estimator under-report by reaching for
        Strategy B (per-group EXPLICIT) where the runtime would do
        Strategy C (broadcast formula + sparse NONE overrides). Pinned
        by ``test_known_group_ids_from_ctx_reads_repo_directly``.
        """
        try:
            repo = getattr(ctx.rl_instance, "device_repository", None) if ctx.rl_instance else None
            if repo is None:
                return []
            ids: set[int] = set()
            for d in repo.list():
                gid = getattr(d, "groupId", None)
                if isinstance(gid, int) and 0 <= gid <= 254:
                    ids.add(gid)
            return sorted(ids)
        except Exception:
            # swallow-ok: optimizer has a no-known-devices fallback;
            # the cost estimate is best-effort observability, not a
            # hard contract. Logged at debug so a recurring failure
            # (e.g. attribute renamed away) can still be tracked
            # without spamming the warning log.
            logger.debug(
                "_known_group_ids_from_ctx failed; estimator falling back",
                exc_info=True,
            )
            return []

    def _rl_preset_lookup_for_estimator():
        """Mirror ``_lookup_rl_preset`` from the runner so the estimator
        can resolve the same references the runner would. Returns ``None``
        if the rl-presets service isn't wired (estimator falls back to the
        action's own params, under-reporting but never crashing)."""
        if rl_presets_service is None:
            return None
        def lookup(ref):
            try:
                if isinstance(ref, str) and ref.startswith("RL:"):
                    return rl_presets_service.get(ref[3:])
                if isinstance(ref, int):
                    return rl_presets_service.get_by_id(ref)
                if isinstance(ref, str):
                    stripped = ref.strip()
                    if stripped.isdigit():
                        return rl_presets_service.get_by_id(int(stripped))
                    return rl_presets_service.get(stripped)
            except Exception:
                # swallow-ok: estimate path never blocks the editor.
                # Debug-level log so a recurring lookup failure can be
                # diagnosed without polluting the warning log on every
                # cost-estimate call.
                logger.debug(
                    "rl_preset lookup failed for ref=%r", ref, exc_info=True,
                )
                return None
            return None
        return lookup

    def _device_lookup_for_estimator():
        """Mirror the runner's ``controller.getDeviceFromAddress`` so
        device-target body sizing in the cost estimator picks up the
        device's stored ``groupId`` (matches the runner's "single-
        device pinned rule" from the broadcast ruleset). Returns
        ``None`` when the controller isn't wired — the planner then
        treats device targets as degraded, matching the runner."""
        rl = ctx.rl_instance
        if rl is None:
            return None
        return getattr(rl, "getDeviceFromAddress", None)

    def _scene_cost_payload(scene_dict) -> dict:
        cost = estimate_scene(scene_dict,
                              known_group_ids=_known_group_ids_from_ctx(),
                              rl_preset_lookup=_rl_preset_lookup_for_estimator(),
                              device_lookup=_device_lookup_for_estimator())
        # Surface the scene's resolved broadcast scope so the editor
        # can render the "Fan-out: N gateways" pill and the operator
        # sees which networks an Auto-mode scene would actually reach.
        # ``scene_network_ids`` honours explicit scope (filtered against
        # current network repo) or falls back to the action walk.
        try:
            from ..services.scene_network_scope import scene_network_ids
            resolved_ids = list(
                scene_network_ids(scene_dict, controller=ctx.rl_instance)
            )
        except Exception:
            # swallow-ok: scope resolution is purely additive for the
            # cost payload; a malformed scene must not break the
            # primary cost numbers.
            resolved_ids = []
        scope_mode = "auto"
        scope_field = scene_dict.get("network_scope") if isinstance(scene_dict, dict) else None
        if isinstance(scope_field, dict) and scope_field.get("mode") == "explicit":
            scope_mode = "explicit"
        return {
            "ok": True,
            "total": {
                "packets":       cost.total.packets,
                "bytes":         cost.total.bytes,
                "airtime_ms":    cost.total.airtime_ms,
                "wall_clock_ms": cost.total.wall_clock_ms,
            },
            "per_action": [
                {
                    "packets":       a.packets,
                    "bytes":         a.bytes,
                    "airtime_ms":    a.airtime_ms,
                    "wall_clock_ms": a.wall_clock_ms,
                    "detail":        a.detail or {},
                }
                for a in cost.per_action
            ],
            "lora": lora_parameters(),
            "resolved_network_ids": resolved_ids,
            "network_scope_mode": scope_mode,
        }

    @bp.route("/api/scenes/<key>/estimate", methods=["GET"])
    def api_scenes_estimate(key):
        """Return projected wire cost (packets, bytes, airtime) for a saved
        scene. The editor uses this to render the per-action cost badge and
        the scene-level total."""
        if scenes_service is None:
            return _scenes_unavailable()
        scene = scenes_service.get(key)
        if scene is None:
            return jsonify({"ok": False, "error": "scene not found"}), 404
        return jsonify(_scene_cost_payload(scene))

    @bp.route("/api/scenes/estimate", methods=["POST"])
    def api_scenes_estimate_draft():
        """Estimate cost for an unsaved draft. Body shape mirrors POST/PUT
        scene: ``{label?, actions: [...]}``. Validates the actions through
        the canonical validator (so the operator sees errors immediately
        on bad input) and then runs the estimator on the canonical form."""
        if scenes_service is None:
            return _scenes_unavailable()
        body = request.get_json(silent=True) or {}
        try:
            # Round-trip the actions through the validator without touching
            # storage. ``replace_all`` is too heavy; we only need canonical
            # actions, so we build a fake scene dict.
            from ..services.scenes_service import (
                _canonical_actions,
                _canonical_network_scope,
            )  # local imports
            canonical_actions = _canonical_actions(body.get("actions") or [])
            canonical_scope = _canonical_network_scope(body.get("network_scope"))
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        scene_dict = {
            "label": (body.get("label") or "").strip() or "draft",
            "actions": canonical_actions,
            "network_scope": canonical_scope,
        }
        return jsonify(_scene_cost_payload(scene_dict))

    @bp.route("/api/scenes/<key>/run", methods=["POST"])
    def api_scenes_run(key):
        """Run a scene synchronously and return the per-action result.

        v1: synchronous request. The HTTP response holds open until the
        runner finishes. ``delay`` actions are capped at 60 s each by the
        service validator, and total scenes are bounded at 20 actions, so
        worst-case wall time is 20 minutes — but realistic scenes finish in
        seconds.

        R7: per-action progress is emitted on the SSE bus (topic
        ``scene_progress``) before each action starts and after it returns.
        The bus is a separate connection from this request so broadcasting
        during the synchronous run does not block the response. The
        Vue editor (``frontend/src/components/scenes/SceneRunPipStrip.vue``)
        updates per-row pips live; the post-run result strip still comes
        from the JSON payload returned here.

        Ephemeral-draft path: when the request body contains an ``actions``
        list, the runner executes that list instead of the persisted scene.
        Nothing is written to storage — the saved scene under ``key`` is
        untouched. ``scene_key`` is still ``key`` so SSE progress events
        resolve in the right editor tab. The body shape mirrors POST /scenes
        / PUT /scenes/<key>: ``{label?, actions, stop_on_error?}``. Used by
        the editor's Run button to execute the displayed draft without
        forcing a save (only the explicit Save button persists).
        """
        if scenes_service is None:
            return _scenes_unavailable()
        if scene_runner_service is None:
            return _runner_unavailable()

        body = request.get_json(silent=True) or {}
        draft_actions = body.get("actions")

        def _emit_progress(payload):
            ctx.sse.broadcast("scene_progress", payload)

        if draft_actions is not None:
            try:
                from ..services.scenes_service import (
                    _canonical_actions,
                    _canonical_network_scope,
                )  # local import
                canonical_actions = _canonical_actions(draft_actions)
            except ValueError as ex:
                return jsonify({"ok": False, "error": str(ex)}), 400
            # stop_on_error + network_scope resolution: explicit body
            # value wins; otherwise fall back to the persisted scene
            # so the saved settings still apply to a draft run when
            # the editor hasn't touched them. Fetch the saved scene
            # once and reuse for both fields. Default True / auto-mode
            # if neither exists — matches the saved-scene defaults.
            saved = None
            if "stop_on_error" in body:
                stop_on_error = bool(body.get("stop_on_error"))
            else:
                saved = scenes_service.get(key)
                stop_on_error = bool(saved.get("stop_on_error", True)) if saved else True
            if "network_scope" in body:
                try:
                    scope = _canonical_network_scope(body.get("network_scope"))
                except ValueError as ex:
                    return jsonify({"ok": False, "error": str(ex)}), 400
            else:
                if saved is None:
                    saved = scenes_service.get(key)
                scope = (
                    (saved.get("network_scope") if saved else None)
                    or {"mode": "auto"}
                )
            scene_dict = {
                "key": key,
                "label": (body.get("label") or "draft").strip() or "draft",
                "actions": canonical_actions,
                "stop_on_error": stop_on_error,
                # Pin the scope so the runner's broadcast fan-out
                # respects the operator's choice on a draft Run too —
                # not just on a saved-scene Run. Without this the
                # runner falls through to auto-mode, which on a
                # broadcast action resolves to every known network.
                "network_scope": scope,
            }
            result = scene_runner_service.run(
                key, progress_cb=_emit_progress, scene=scene_dict,
            )
        else:
            result = scene_runner_service.run(key, progress_cb=_emit_progress)
        if not result.ok and result.error == "scene_not_found":
            return jsonify(result.to_dict()), 404
        return jsonify({"ok": result.ok, "result": result.to_dict()})

    @bp.route("/api/fw/uploads", methods=["GET"])
    def api_fw_uploads():
        return jsonify({"ok": True, "files": ota_service.list_uploads()})

    @bp.route("/api/wifi/interfaces", methods=["GET"])
    def api_wifi_interfaces():
        return jsonify({"ok": True, "ifaces": host_wifi_service.wifi_interfaces()})

    @bp.route("/api/presets/download", methods=["POST"])
    def api_presets_download():
        ctx.sse.ensure_transport_hooked(ctx.rl_instance)
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        mac = str(body.get("mac") or "").strip()
        if not mac:
            return jsonify({"ok": False, "error": "missing mac"}), 400
        try:
            wifi = parse_wifi_options(body, ota_service)
        except RequestParseError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400

        expected_mac = ota_service.expected_mac_hex(mac)
        if not expected_mac:
            return jsonify({"ok": False, "error": "invalid mac"}), 400

        def do_presets_download():
            result = ota_workflows.download_presets(
                rl_instance=ctx.rl_instance,
                task_manager=ctx.tasks,
                mac=mac,
                base_url=wifi["base_url"],
                wifi=wifi,
                host_wifi_enable=wifi["host_wifi_enable"],
                host_wifi_restore=wifi["host_wifi_restore"],
            )
            # The workflow runs in the task thread; ``ctx.sse.broadcast``
            # is thread-safe (snapshot-then-fan-out under
            # ``_clients_lock``). Broadcast WLED_PRESETS only when the
            # file actually landed on disk — failures already surface
            # via the task's error state.
            if isinstance(result, dict) and result.get("ok"):
                _sse_refresh(ctx, {state_scope.WLED_PRESETS})
            return result

        task = ctx.tasks.start("presets_download", do_presets_download, meta={"stage": "INIT", "addr": mac, "message": "Preset download started", "baseUrl": wifi["base_url"]})
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    @bp.route("/api/fw/start", methods=["POST"])
    def api_fw_start():
        ctx.sse.ensure_transport_hooked(ctx.rl_instance)
        if ctx.tasks.is_running():
            return ctx.tasks.busy_response()

        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        if not isinstance(macs, list) or not macs:
            return jsonify({"ok": False, "error": "missing macs"}), 400

        do_firmware = bool(body.get("doFirmware", True))
        do_presets = bool(body.get("doPresets", False))
        do_cfg = bool(body.get("doCfg", False))
        if not (do_firmware or do_presets or do_cfg):
            return jsonify({"ok": False, "error": "no operations selected"}), 400

        fw_info = ota_service.get_upload(str(body.get("fwId") or "").strip(), expect_kind="firmware") if do_firmware else None
        if do_firmware and not fw_info:
            return jsonify({"ok": False, "error": "firmware file not uploaded (fwId)"}), 400

        presets_info = None
        if do_presets:
            presets_name = str(body.get("presetsName") or "").strip()
            presets_path = presets_service.preset_path_for_name(presets_name) if presets_name else None
            if not presets_path:
                return jsonify({"ok": False, "error": "presets file not found"}), 400
            presets_info = presets_service.file_info(presets_path, name=presets_name)

        cfg_info = ota_service.get_upload(str(body.get("cfgId") or "").strip(), expect_kind="cfg") if do_cfg else None
        if do_cfg and not cfg_info:
            return jsonify({"ok": False, "error": "cfg file not uploaded (cfgId)"}), 400

        try:
            retries = int(body.get("retries") or 3)
        except (TypeError, ValueError):
            # swallow-ok: bad input -> sane default. Narrow the catch
            # so an unrelated bug elsewhere in this block surfaces as
            # a 500 instead of being silently defaulted.
            retries = 3
        retries = max(1, min(retries, 10))
        try:
            wifi = parse_wifi_options(body, ota_service)
        except RequestParseError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400
        stop_on_error = bool(body.get("stopOnError") or False)
        # Cross-fork-migration escape hatch. Forwarded into the multipart
        # body as ``skipValidation=1`` so WLED's ``ota_update.cpp:139``
        # bypasses the release-name check. Off by default (the safety
        # check exists for a reason); operator ticks it explicitly when
        # migrating between firmware forks.
        skip_validation = bool(body.get("skipValidation") or False)

        def do_fwupdate():
            return ota_workflows.run_firmware_update(
                rl_instance=ctx.rl_instance,
                task_manager=ctx.tasks,
                devices_provider=ctx.devices,
                macs=macs,
                base_url=wifi["base_url"],
                fw_info=fw_info,
                presets_info=presets_info,
                cfg_info=cfg_info,
                retries=retries,
                stop_on_error=stop_on_error,
                wifi=wifi,
                host_wifi_enable=wifi["host_wifi_enable"],
                host_wifi_restore=wifi["host_wifi_restore"],
                skip_validation=skip_validation,
            )

        # ``macs`` + ``deviceState`` are the authoritative per-device
        # row identity / state surface for the WebUI's progress panel —
        # see frontend/POST_MIGRATION_CLEANUP.md §9. The workflow
        # mutates these in place across the per-device loop and emits
        # them on every meta update.
        addrs = [str(m) for m in macs]
        task = ctx.tasks.start(
            "fwupdate",
            do_fwupdate,
            meta={
                "stage": "INIT",
                "index": 0,
                "total": len(macs),
                "retries": retries,
                "addr": None,
                "message": "Firmware update started",
                "baseUrl": wifi["base_url"],
                "macs": addrs,
                "deviceState": {a: "queued" for a in addrs},
            },
        )
        if not task:
            return ctx.tasks.busy_response()
        return jsonify({"ok": True, "task": task})

    return {"ensure_presets_loaded": presets_service.ensure_loaded}
