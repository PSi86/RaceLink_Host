from __future__ import annotations

import json
import time

from flask import Response, jsonify, request, stream_with_context, templating


def register_runtime_routes(bp, *, rhapi, rl_instance, rl_devicelist, rl_grouplist, RL_DeviceGroup, rl_lock, queue_factory,
                            state, task_runner, transport_hooks, serializers, helpers):
    @bp.route("/racelink")
    def rl_render():
        transport_hooks.ensure_hooked()
        return templating.render_template(
            "racelink.html",
            serverInfo=None,
            getOption=rhapi.db.option,
            __=rhapi.__,
        )

    @bp.route("/racelink/api/events")
    def api_events():
        transport_hooks.ensure_hooked()
        q = queue_factory()
        state.add_client(q)
        try:
            q.put(("master", state.master_snapshot()), timeout=0.01)
            q.put(("task", state.task_snapshot()), timeout=0.01)
        except Exception:
            pass

        def _encode(event_name: str, payload) -> str:
            return f"event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"

        @stream_with_context
        def gen():
            last_ping = time.time()
            try:
                while True:
                    try:
                        item = q.get(timeout=1.0)
                    except Exception:
                        item = None
                    now = time.time()
                    if item is None:
                        if now - last_ping >= 15.0:
                            last_ping = now
                            yield ": ping\n\n"
                        continue
                    ev_name, payload = item
                    yield _encode(ev_name, payload)
            finally:
                state.remove_client(q)

        return Response(gen(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })

    @bp.route("/racelink/api/devices", methods=["GET"])
    def api_devices():
        with rl_lock:
            rows = [serializers["device"](d) for d in rl_devicelist]
        return jsonify({"ok": True, "devices": rows})

    @bp.route("/racelink/api/specials", methods=["GET"])
    def api_specials():
        context = {"rl_instance": rl_instance}
        return jsonify({"ok": True, "specials": helpers["specials_config"](context=context, serialize_ui=True)})

    @bp.route("/racelink/api/groups", methods=["GET"])
    def api_groups():
        with rl_lock:
            group_rows = [{"id": 0, "name": "Unconfigured", "static": False, "dev_type": 0,
                           "device_count": int(serializers["group_counts"]().get(0, 0))}]
            counts = serializers["group_counts"]()
            for i, g in enumerate(rl_grouplist):
                name = getattr(g, "name", f"Group {i}")
                if str(name).strip().lower() in {"unconfigured", "all wled nodes", "all wled devices"}:
                    continue
                group_rows.append({
                    "id": i,
                    "name": name,
                    "static": bool(getattr(g, "static_group", 0)),
                    "dev_type": int(getattr(g, "dev_type", 0) or 0),
                    "device_count": int(counts.get(i, 0)),
                })
        return jsonify({"ok": True, "groups": group_rows})

    @bp.route("/racelink/api/master", methods=["GET"])
    def api_master():
        return jsonify({"ok": True, "master": state.master_snapshot(), "task": state.task_snapshot()})

    @bp.route("/racelink/api/task", methods=["GET"])
    def api_task():
        return jsonify({"ok": True, "task": state.task_snapshot()})

    @bp.route("/racelink/api/options", methods=["GET"])
    def api_options():
        opts = helpers["effect_select_options"](context={"rl_instance": rl_instance})
        return jsonify({"ok": True, "effects": opts})

    @bp.route("/racelink/api/discover", methods=["POST"])
    def api_discover():
        transport_hooks.ensure_hooked()
        if state.task_is_running():
            return helpers["busy_response"](state.task_snapshot())

        body = request.get_json(silent=True) or {}
        target_gid = body.get("targetGroupId")
        new_group_name = body.get("newGroupName")

        created_gid = None
        with rl_lock:
            if new_group_name:
                rl_grouplist.append(RL_DeviceGroup(str(new_group_name), static_group=0, dev_type=0))
                created_gid = len(rl_grouplist) - 1
                helpers["log"](f"RaceLink: Created group '{new_group_name}' (id={created_gid})")
            if target_gid is None and created_gid is not None:
                target_gid = created_gid

        def do_discover():
            add_to_group = -1
            if target_gid not in (None, 0, "0"):
                add_to_group = int(target_gid)
            n_found = int(rl_instance.getDevices(groupFilter=0, addToGroup=add_to_group) or 0)
            return {"found": n_found, "createdGroupId": created_gid, "targetGroupId": target_gid}

        t = task_runner.start_task("discover", do_discover, meta={"createdGroupId": created_gid, "targetGroupId": target_gid})
        if not t:
            return helpers["busy_response"](state.task_snapshot())
        return jsonify({"ok": True, "task": t})

    @bp.route("/racelink/api/status", methods=["POST"])
    def api_status():
        transport_hooks.ensure_hooked()
        if state.task_is_running():
            return helpers["busy_response"](state.task_snapshot())

        body = request.get_json(silent=True) or {}
        selection = body.get("selection") or body.get("macs") or []
        group_id = body.get("groupId")

        def do_status():
            updated = 0
            if selection:
                if hasattr(rl_instance, "getStatusSelection"):
                    updated = int(rl_instance.getStatusSelection(selection) or 0)
                else:
                    for mac in selection:
                        dev = rl_instance.getDeviceFromAddress(mac)
                        if dev:
                            updated += int(rl_instance.getStatus(targetDevice=dev) or 0)
            elif group_id is not None:
                updated = int(rl_instance.getStatus(groupFilter=int(group_id)) or 0)
            else:
                updated = int(rl_instance.getStatus(groupFilter=255) or 0)
            return {"updated": updated, "groupId": group_id, "selectionCount": len(selection) if selection else 0}

        meta = {"groupId": group_id, "selectionCount": len(selection) if selection else 0}
        t = task_runner.start_task("status", do_status, meta=meta)
        if not t:
            return helpers["busy_response"](state.task_snapshot())
        return jsonify({"ok": True, "task": t})
