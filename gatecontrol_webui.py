"""
GateControl WebUI (importable module)
-------------------------------------
Usage in your plugin's __init__.py:

    from .gatecontrol_webui import register_gc_blueprint

    def initialize(rhapi):
        global gc_instance, gc_devicelist, gc_grouplist, GC_DeviceGroup, logger
        # ... your existing setup ...
        register_gc_blueprint(
            rhapi,
            gc_instance=gc_instance,
            gc_devicelist=gc_devicelist,
            gc_grouplist=gc_grouplist,
            GC_DeviceGroup=GC_DeviceGroup,
            logger=logger
        )

This will register the page at /gatecontrol and the JSON endpoints under /gatecontrol/api/*.
"""

from flask import Blueprint, request, jsonify, templating

# Use gevent lock if available, otherwise fallback to threading.Lock
try:
    from gevent.lock import Semaphore as _GCLock
    _DefaultLock = _GCLock
except Exception:  # pragma: no cover
    try:
        from gevent.lock import RLock as _GCLock  # alt name on some envs
        _DefaultLock = _GCLock
    except Exception:
        import threading
        _DefaultLock = threading.Lock


def register_gc_blueprint(
    rhapi,
    *,
    gc_instance,
    gc_devicelist,
    gc_grouplist,
    GC_DeviceGroup,
    logger=None
):
    """
    Register the GateControl blueprint with the given RotorHazard API and GateControl context.
    All references are passed explicitly to avoid tight coupling with __init__.py globals.
    """

    _gc_lock = _DefaultLock()

    def _log(msg):
        try:
            if logger:
                logger.info(msg)
            else:
                print(msg)
        except Exception:
            print(msg)

    def _gc_serialize_device(dev):
        """Make GC_Device JSON-serializable for the UI table."""
        import time as _time
        online = None
        try:
            if getattr(dev, "last_seen_ts", 0):
                online = (_time.time() - float(dev.last_seen_ts) < 20.0)
        except Exception:
            online = None
        d = {
            "addr": getattr(dev, "addr", None),
            "name": getattr(dev, "name", None),
            "type": int(getattr(dev, "type", 0) or 0),
            "groupId": int(getattr(dev, "groupId", 0) or 0),
            "state": int(getattr(dev, "state", 0) or 0),
            "effect": int(getattr(dev, "effect", 0) or 0),
            "brightness": int(getattr(dev, "brightness", 0) or 0),
            "voltage_mV": int(getattr(dev, "voltage_mV", 0) or 0),
            "node_rssi": int(getattr(dev, "node_rssi", 0) or 0),
            "node_snr": int(getattr(dev, "node_snr", 0) or 0),
            "host_rssi": int(getattr(dev, "host_rssi", 0) or 0),
            "host_snr": int(getattr(dev, "host_snr", 0) or 0),
            "version": int(getattr(dev, "version", 0) or 0),
            "caps": int(getattr(dev, "caps", 0) or 0),
            "last_seen_ts": float(getattr(dev, "last_seen_ts", 0.0) or 0.0),
            "last_ack": getattr(dev, "last_ack", None),
            "online": online,
        }
        return d

    def _gc_group_counts():
        """Return device counts per groupId for quick display."""
        counts = {}
        try:
            for dev in gc_devicelist:
                gid = int(getattr(dev, "groupId", 0) or 0)
                counts[gid] = counts.get(gid, 0) + 1
        except Exception:
            pass
        return counts

    bp = Blueprint(
        "gatecontrol",
        __name__,
        template_folder="pages",
        static_folder="static",
        static_url_path="/gatecontrol/static"
    )

    # -----------------------
    # Page
    # -----------------------
    @bp.route("/gatecontrol")
    def gc_render():
        return templating.render_template(
            "gatecontrol.html",
            serverInfo=None,
            getOption=rhapi.db.option,
            __=rhapi.__
        )

    # -----------------------
    # JSON API: Read
    # -----------------------
    @bp.route("/gatecontrol/api/devices", methods=["GET"])
    def api_devices():
        with _gc_lock:
            rows = [_gc_serialize_device(d) for d in gc_devicelist]
        return jsonify({"ok": True, "devices": rows})

    @bp.route("/gatecontrol/api/groups", methods=["GET"])
    def api_groups():
        with _gc_lock:
            group_rows = []
            counts = _gc_group_counts()
            for i, g in enumerate(gc_grouplist):
                group_rows.append({
                    "id": i,
                    "name": getattr(g, "name", f"Group {i}"),
                    "static": bool(getattr(g, "static_group", 0)),
                    "device_type": int(getattr(g, "device_type", 0) or 0),
                    "device_count": int(counts.get(i, 0)),
                })
        return jsonify({"ok": True, "groups": group_rows})

    @bp.route("/gatecontrol/api/options", methods=["GET"])
    def api_options():
        # Try to extract effect options from uiEffectList if present
        eff = []
        try:
            for opt in gc_instance.uiEffectList:
                val = getattr(opt, "value", None)
                lab = getattr(opt, "label", None) or getattr(opt, "name", None) or str(opt)
                if val is None:
                    continue
                eff.append({"value": str(val), "label": str(lab)})
        except Exception:
            eff = []
        return jsonify({"ok": True, "effects": eff})

    # -----------------------
    # JSON API: Actions
    # -----------------------
    @bp.route("/gatecontrol/api/discover", methods=["POST"])
    def api_discover():
        body = request.get_json(silent=True) or {}
        target_gid = body.get("targetGroupId", None)
        new_group_name = body.get("newGroupName", None)

        created_gid = None
        with _gc_lock:
            if new_group_name:
                # create new non-static group at the end, device_type default 0
                g = GC_DeviceGroup(str(new_group_name), static_group=0, device_type=0)
                gc_grouplist.append(g)
                created_gid = len(gc_grouplist) - 1
                _log(f"GateControl: Created group '{new_group_name}' (id={created_gid})")
            if target_gid is None and created_gid is not None:
                target_gid = created_gid

        # Discovery: ask nodes with groupId=0 and assign to group
        n_found = 0
        try:
            n_found = int(gc_instance.getDevices(groupFilter=0, addToGroup=int(target_gid) if target_gid is not None else -1) or 0)
        except Exception as ex:
            _log(f"GateControl: discover failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500

        return jsonify({"ok": True, "found": n_found, "createdGroupId": created_gid})

    @bp.route("/gatecontrol/api/status", methods=["POST"])
    def api_status():
        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        group_id = body.get("groupId", None)

        updated = 0
        try:
            if macs:
                # Per-device status requests
                for mac in macs:
                    dev = gc_instance.getDeviceFromAddress(mac)
                    if dev:
                        updated += int(gc_instance.getStatus(targetDevice=dev) or 0)
            elif group_id is not None:
                updated = int(gc_instance.getStatus(groupFilter=int(group_id)) or 0)
            else:
                # All
                updated = int(gc_instance.getStatus(groupFilter=255) or 0)
        except Exception as ex:
            _log(f"GateControl: getStatus failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500

        return jsonify({"ok": True, "updated": updated})

    @bp.route("/gatecontrol/api/devices/update-meta", methods=["POST"])
    def api_devices_update_meta():
        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        new_group = body.get("groupId", None)
        new_name = body.get("name", None)

        changed = 0
        with _gc_lock:
            for mac in macs:
                dev = gc_instance.getDeviceFromAddress(mac)
                if not dev:
                    continue
                if new_name and isinstance(new_name, str) and macs and len(macs) == 1:
                    dev.name = new_name
                    changed += 1
                if new_group is not None:
                    try:
                        # persist on device & in memory
                        gc_instance.setGateGroupId(dev, int(new_group))
                        changed += 1
                    except Exception as ex:
                        _log(f"GateControl: setGateGroupId failed for {mac}: {ex}")
        try:
            gc_instance.save_to_db({"manual": True})
        except Exception:
            pass
        return jsonify({"ok": True, "changed": changed})

    @bp.route("/gatecontrol/api/groups/create", methods=["POST"])
    def api_groups_create():
        body = request.get_json(silent=True) or {}
        name = str(body.get("name", "")).strip()
        device_type = int(body.get("device_type", 0) or 0)
        if not name:
            return jsonify({"ok": False, "error": "name required"}), 400
        with _gc_lock:
            gc_grouplist.append(GC_DeviceGroup(name, static_group=0, device_type=device_type))
            gid = len(gc_grouplist) - 1
            try:
                gc_instance.save_to_db({"manual": True})
            except Exception:
                pass
        return jsonify({"ok": True, "id": gid})

    @bp.route("/gatecontrol/api/groups/rename", methods=["POST"])
    def api_groups_rename():
        body = request.get_json(silent=True) or {}
        gid = int(body.get("id"))
        name = str(body.get("name", "")).strip()
        with _gc_lock:
            if gid < 0 or gid >= len(gc_grouplist):
                return jsonify({"ok": False, "error": "invalid group id"}), 400
            g = gc_grouplist[gid]
            if getattr(g, "static_group", 0):
                return jsonify({"ok": False, "error": "static group"}), 400
            g.name = name or g.name
            try:
                gc_instance.save_to_db({"manual": True})
            except Exception:
                pass
        return jsonify({"ok": True})

    @bp.route("/gatecontrol/api/groups/delete", methods=["POST"])
    def api_groups_delete():
        body = request.get_json(silent=True) or {}
        gid = int(body.get("id"))
        with _gc_lock:
            if gid < 0 or gid >= len(gc_grouplist):
                return jsonify({"ok": False, "error": "invalid group id"}), 400
            g = gc_grouplist[gid]
            if getattr(g, "static_group", 0):
                return jsonify({"ok": False, "error": "static group"}), 400
            # must be empty
            for d in gc_devicelist:
                if int(getattr(d, "groupId", -1)) == gid:
                    return jsonify({"ok": False, "error": "group not empty"}), 400
            del gc_grouplist[gid]
            try:
                gc_instance.save_to_db({"manual": True})
            except Exception:
                pass
        return jsonify({"ok": True})

    @bp.route("/gatecontrol/api/groups/force", methods=["POST"])
    def api_groups_force():
        try:
            gc_instance.forceGroups(args=None, sanityCheck=True)
        except Exception as ex:
            _log(f"GateControl: forceGroups failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500
        return jsonify({"ok": True})

    @bp.route("/gatecontrol/api/save", methods=["POST"])
    def api_save():
        try:
            gc_instance.save_to_db({"manual": True})
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        return jsonify({"ok": True})

    @bp.route("/gatecontrol/api/reload", methods=["POST"])
    def api_reload():
        try:
            gc_instance.load_from_db()
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        return jsonify({"ok": True})

    @bp.route("/gatecontrol/api/devices/control", methods=["POST"])
    def api_devices_control():
        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        state = body.get("state", None)
        effect = body.get("effect", None)
        brightness = body.get("brightness", None)
        group_id = body.get("groupId", None)

        def _toint(x, default=None):
            try:
                return int(x)
            except Exception:
                return default
        state = _toint(state, None); effect = _toint(effect, None); brightness = _toint(brightness, None)

        changed = 0
        try:
            if group_id is not None and state is not None and effect is not None and brightness is not None:
                gc_instance.sendGroupControl(int(group_id), state, effect, brightness)
                changed += 1
            elif macs and state is not None and effect is not None and brightness is not None:
                for mac in macs:
                    dev = gc_instance.getDeviceFromAddress(mac)
                    if dev:
                        gc_instance.sendGateControl(dev, state, effect, brightness)
                        changed += 1
            else:
                return jsonify({"ok": False, "error": "missing parameters"}), 400
        except Exception as ex:
            _log(f"GateControl: control failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500

        return jsonify({"ok": True, "changed": changed})

    # Finally register
    rhapi.ui.blueprint_add(bp)
    _log("GateControl UI blueprint registered at /gatecontrol")