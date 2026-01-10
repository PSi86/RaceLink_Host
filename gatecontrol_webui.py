"""
GateControl WebUI (importable module)
-------------------------------------

This module registers a Flask blueprint for the GateControl LoRa plugin.

Key goals:
- No periodic polling required (uses Server-Sent Events / SSE for live UI state)
- "Busy" protection: only one long-running radio task at a time (discover/status)
- UI can show master activity (TX pending / RX window open) based on USB events
- UI can show task progress (RX reply counts) based on LoRa/USB events

Usage in your plugin's __init__.py:

    from .gatecontrol_webui import register_gc_blueprint

    def initialize(rhapi):
        ...
        register_gc_blueprint(
            rhapi,
            gc_instance=gc_instance,
            gc_devicelist=gc_devicelist,
            gc_grouplist=gc_grouplist,
            GC_DeviceGroup=GC_DeviceGroup,
            logger=logger
        )

This registers the page at /gatecontrol and JSON endpoints under /gatecontrol/api/*.
"""

from __future__ import annotations

from typing import Optional

import json
import time
import threading

from flask import Blueprint, request, jsonify, templating, Response, stream_with_context

# Use gevent lock/queue if available, otherwise fallback to threading primitives
try:
    from gevent.lock import Semaphore as _GCLock  # type: ignore
    _DefaultLock = _GCLock
except Exception:  # pragma: no cover
    try:
        from gevent.lock import RLock as _GCLock  # type: ignore
        _DefaultLock = _GCLock
    except Exception:  # pragma: no cover
        _DefaultLock = threading.Lock

try:
    from gevent.queue import Queue as _GCQueue  # type: ignore
except Exception:  # pragma: no cover
    try:
        from queue import Queue as _GCQueue  # type: ignore
    except Exception:  # pragma: no cover
        _GCQueue = None  # should never happen


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
    Register the GateControl blueprint with RotorHazard.

    All references are passed explicitly to avoid tight coupling with __init__.py globals.
    """

    _gc_lock = _DefaultLock()
    _clients_lock = _DefaultLock()
    _task_lock = _DefaultLock()

    # --- helpers ---
    def _log(msg):
        try:
            if logger:
                logger.info(msg)
            else:
                print(msg)
        except Exception:
            print(msg)

    # --- Master state mirrored to UI ---
    _master = {
        "state": "IDLE",              # IDLE | TX | RX | ERROR
        "tx_pending": False,
        "rx_window_open": False,
        "rx_window_ms": 0,
        "last_event": None,
        "last_event_ts": 0.0,
        "last_tx_len": 0,
        "last_rx_count_delta": 0,
        "last_error": None,
    }

    # --- Task state (one at a time) ---
    _task = None  # dict or None
    _task_seq = 0

    # --- SSE clients ---
    _clients = set()  # set[Queue]

    def _master_snapshot():
        return dict(_master)

    def _task_snapshot():
        with _task_lock:
            return dict(_task) if _task else None

    def _broadcast(ev_name: str, payload):
        # Fan-out to all connected SSE clients
        with _clients_lock:
            dead = []
            for q in list(_clients):
                try:
                    q.put((ev_name, payload), timeout=0.01)
                except Exception:
                    dead.append(q)
            for q in dead:
                try:
                    _clients.remove(q)
                except Exception:
                    pass

    def _set_master(**updates):
        changed = False
        for k, v in updates.items():
            if _master.get(k) != v:
                _master[k] = v
                changed = True
        if changed:
            _master["last_event_ts"] = time.time()
            _broadcast("master", _master_snapshot())

    def _set_task(new_task: Optional[dict]):
        nonlocal _task
        with _task_lock:
            _task = new_task
        _broadcast("task", _task_snapshot())

    def _task_update(**updates):
        with _task_lock:
            if not _task:
                return
            for k, v in updates.items():
                _task[k] = v
        _broadcast("task", _task_snapshot())

    def _task_is_running() -> bool:
        with _task_lock:
            return bool(_task and _task.get("state") == "running")

    def _task_busy_response():
        snap = _task_snapshot()
        return jsonify({"ok": False, "busy": True, "task": snap}), 409

    # --- Transport event hookup (LoRaUSB.on_event) ---
    _hooked_lora = {"ok": False}

    def _ensure_transport_hooked():
        """
        Attach a callback to gc_instance.lora.on_event so we can update master/task state
        and feed SSE clients, without breaking any existing callback.
        """
        if _hooked_lora["ok"]:
            return
        lora = getattr(gc_instance, "lora", None)
        if not lora or not hasattr(lora, "on_event"):
            return

        prev = getattr(lora, "on_event", None)

        def _mux(ev: dict):
            # 1) our handler
            try:
                _on_transport_event(ev)
            except Exception:
                pass
            # 2) previous handler (if any)
            try:
                if prev and prev is not _mux:
                    prev(ev)
            except Exception:
                pass

        try:
            lora.on_event = _mux
            _hooked_lora["ok"] = True
            _log("GateControl: transport event hook installed")
        except Exception as ex:
            _log(f"GateControl: transport hook failed: {ex}")

    # Event type constants from gc_transport (optional)
    try:
        from .gc_transport import EV_ERROR, EV_RX_WINDOW_OPEN, EV_RX_WINDOW_CLOSED, EV_TX_DONE  # type: ignore
    except Exception:
        try:
            from gc_transport import EV_ERROR, EV_RX_WINDOW_OPEN, EV_RX_WINDOW_CLOSED, EV_TX_DONE  # type: ignore
        except Exception:
            EV_ERROR = 0xF0
            EV_RX_WINDOW_OPEN = 0xF1
            EV_RX_WINDOW_CLOSED = 0xF2
            EV_TX_DONE = 0xF3

    def _on_transport_event(ev: dict):
        """
        Receive USB transport events (EV_*) and LoRa reply events and update UI state.
        """
        t = ev.get("type", None)

        # USB-only events
        if t == EV_RX_WINDOW_OPEN:
            _set_master(
                state="RX",
                rx_window_open=True,
                rx_window_ms=int(ev.get("window_ms", 0) or 0),
                last_event="RX_WINDOW_OPEN",
                last_error=None,
            )
            if _task_is_running():
                _task_update(rx_windows=int((_task_snapshot() or {}).get("rx_windows", 0)) + 1)
            return

        if t == EV_RX_WINDOW_CLOSED:
            delta = int(ev.get("rx_count_delta", 0) or 0)
            # If no TX pending, fall back to IDLE. (TX_DONE will override if needed.)
            _set_master(
                state="TX" if _master.get("tx_pending") else "IDLE",
                rx_window_open=False,
                rx_window_ms=0,
                last_event="RX_WINDOW_CLOSED",
                last_rx_count_delta=delta,
                last_error=None,
            )
            if _task_is_running():
                snap = _task_snapshot() or {}
                _task_update(
                    rx_count_delta_total=int(snap.get("rx_count_delta_total", 0)) + delta,
                    rx_windows=int(snap.get("rx_windows", 0)) + 1,
                )
            return

        if t == EV_TX_DONE:
            _set_master(
                tx_pending=False,
                state="RX" if _master.get("rx_window_open") else "IDLE",
                last_event="TX_DONE",
                last_tx_len=int(ev.get("last_len", 0) or 0),
                last_error=None,
            )
            return

        if t == EV_ERROR:
            # Keep error state visible; tasks can continue but UI should show error
            raw = ev.get("data", b"")
            try:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.hex().upper()
            except Exception:
                pass
            _set_master(
                state="ERROR",
                last_event="USB_ERROR",
                last_error=str(raw),
            )
            if _task_is_running():
                _task_update(last_error=str(raw))
            return

        # LoRa reply events from LoRaUSB parser
        reply = ev.get("reply")
        if not reply:
            return

        # Track replies for running tasks
        if _task_is_running():
            snap = _task_snapshot() or {}
            tname = snap.get("name")
            if tname == "discover" and reply == "IDENTIFY_REPLY":
                _task_update(rx_replies=int(snap.get("rx_replies", 0)) + 1)
            elif tname == "status" and reply == "STATUS_REPLY":
                _task_update(rx_replies=int(snap.get("rx_replies", 0)) + 1)

        # Update master activity hint (we received something)
        _set_master(last_event=reply, last_error=None)

    # --- Serialization ---
    def _gc_serialize_device(dev):
        """Make GC_Device JSON-serializable for the UI table."""
        online = None
        try:
            if getattr(dev, "last_seen_ts", 0):
                online = (time.time() - float(dev.last_seen_ts) < 20.0)
        except Exception:
            online = None

        d = {
            "addr": getattr(dev, "addr", None),
            "name": getattr(dev, "name", None),
            "type": int(getattr(dev, "type", 0) or 0),
            "groupId": int(getattr(dev, "groupId", 0) or 0),

            # new proto v1.2 fields
            "flags": int(getattr(dev, "flags", 0) or 0),
            "presetId": int(getattr(dev, "presetId", 0) or 0),
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
        _ensure_transport_hooked()
        return templating.render_template(
            "gatecontrol.html",
            serverInfo=None,
            getOption=rhapi.db.option,
            __=rhapi.__
        )

    # -----------------------
    # SSE Events
    # -----------------------
    @bp.route("/gatecontrol/api/events")
    def api_events():
        _ensure_transport_hooked()

        q = _GCQueue()
        with _clients_lock:
            _clients.add(q)

        # Push initial snapshots
        try:
            q.put(("master", _master_snapshot()), timeout=0.01)
            q.put(("task", _task_snapshot()), timeout=0.01)
        except Exception:
            pass

        def _encode(event_name: str, payload) -> str:
            return f"event: {event_name}\ndata: {json.dumps(payload, separators=(',',':'))}\n\n"

        @stream_with_context
        def gen():
            # Keep-alive ping every ~15s
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
                with _clients_lock:
                    try:
                        _clients.remove(q)
                    except Exception:
                        pass

        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # harmless without nginx, helpful with proxies
        }
        return Response(gen(), mimetype="text/event-stream", headers=headers)

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

    @bp.route("/gatecontrol/api/master", methods=["GET"])
    def api_master():
        return jsonify({"ok": True, "master": _master_snapshot(), "task": _task_snapshot()})

    @bp.route("/gatecontrol/api/task", methods=["GET"])
    def api_task():
        return jsonify({"ok": True, "task": _task_snapshot()})

    @bp.route("/gatecontrol/api/options", methods=["GET"])
    def api_options():
        # still called "effects" for UI legacy; values can represent preset ids
        opts = []
        try:
            for opt in gc_instance.uiEffectList:
                val = getattr(opt, "value", None)
                lab = getattr(opt, "label", None) or getattr(opt, "name", None) or str(opt)
                if val is None:
                    continue
                opts.append({"value": str(val), "label": str(lab)})
        except Exception:
            opts = []
        return jsonify({"ok": True, "effects": opts})

    # -----------------------
    # JSON API: Actions (Tasks)
    # -----------------------
    def _start_task(name: str, target_fn, meta: Optional[dict] = None):
        nonlocal _task_seq, _task
        with _task_lock:
            if _task and _task.get("state") == "running":
                return None
            _task_seq += 1
            tid = _task_seq
            _task_obj = {
                "id": tid,
                "name": name,
                "state": "running",  # running|done|error
                "started_ts": time.time(),
                "ended_ts": None,
                "meta": meta or {},
                "rx_replies": 0,
                "rx_windows": 0,
                "rx_count_delta_total": 0,
                "last_error": None,
                "result": None,
            }
            _task = _task_obj

        _broadcast("task", _task_snapshot())

        def runner():
            try:
                # hint: something is going out now
                _set_master(state="TX", tx_pending=True, last_event=f"TASK_{name.upper()}_START")
                res = target_fn()
                _task_update(state="done", ended_ts=time.time(), result=res)
                _set_master(state="IDLE" if not _master.get("rx_window_open") else "RX", last_event=f"TASK_{name.upper()}_DONE")
                # Tell UI to refresh lists
                _broadcast("refresh", {"what": ["groups", "devices"]})
            except Exception as ex:
                _task_update(state="error", ended_ts=time.time(), last_error=str(ex))
                _set_master(state="ERROR", last_event=f"TASK_{name.upper()}_ERROR", last_error=str(ex))
            finally:
                pass

        th = threading.Thread(target=runner, daemon=True)
        th.start()
        return _task_snapshot()

    @bp.route("/gatecontrol/api/discover", methods=["POST"])
    def api_discover():
        _ensure_transport_hooked()
        if _task_is_running():
            return _task_busy_response()

        body = request.get_json(silent=True) or {}
        target_gid = body.get("targetGroupId", None)
        new_group_name = body.get("newGroupName", None)

        # group creation is cheap; do it before starting the radio task
        created_gid = None
        with _gc_lock:
            if new_group_name:
                g = GC_DeviceGroup(str(new_group_name), static_group=0, device_type=0)
                gc_grouplist.append(g)
                created_gid = len(gc_grouplist) - 1
                _log(f"GateControl: Created group '{new_group_name}' (id={created_gid})")
            if target_gid is None and created_gid is not None:
                target_gid = created_gid

        def do_discover():
            # Discovery: ask nodes with groupId=0 and assign to group
            n_found = int(gc_instance.getDevices(groupFilter=0, addToGroup=int(target_gid) if target_gid is not None else -1) or 0)
            return {"found": n_found, "createdGroupId": created_gid, "targetGroupId": target_gid}

        t = _start_task("discover", do_discover, meta={"createdGroupId": created_gid, "targetGroupId": target_gid})
        if not t:
            return _task_busy_response()
        return jsonify({"ok": True, "task": t})

    @bp.route("/gatecontrol/api/status", methods=["POST"])
    def api_status():
        _ensure_transport_hooked()
        if _task_is_running():
            return _task_busy_response()

        body = request.get_json(silent=True) or {}
        selection = body.get("selection") or body.get("macs") or []
        group_id = body.get("groupId", None)

        def do_status():
            updated = 0
            if selection:
                # If plugin has getStatusSelection(selection) prefer it
                if hasattr(gc_instance, "getStatusSelection"):
                    updated = int(gc_instance.getStatusSelection(selection) or 0)
                else:
                    for mac in selection:
                        dev = gc_instance.getDeviceFromAddress(mac)
                        if dev:
                            updated += int(gc_instance.getStatus(targetDevice=dev) or 0)
            elif group_id is not None:
                updated = int(gc_instance.getStatus(groupFilter=int(group_id)) or 0)
            else:
                updated = int(gc_instance.getStatus(groupFilter=255) or 0)
            return {"updated": updated, "groupId": group_id, "selectionCount": len(selection) if selection else 0}

        meta = {"groupId": group_id, "selectionCount": len(selection) if selection else 0}
        t = _start_task("status", do_status, meta=meta)
        if not t:
            return _task_busy_response()
        return jsonify({"ok": True, "task": t})

    # -----------------------
    # JSON API: Meta updates (group/name)
    # -----------------------
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
                        gc_instance.setGateGroupId(dev, int(new_group))
                        changed += 1
                    except Exception as ex:
                        _log(f"GateControl: setGateGroupId failed for {mac}: {ex}")
        try:
            gc_instance.save_to_db({"manual": True})
        except Exception:
            pass

        _broadcast("refresh", {"what": ["groups", "devices"]})
        return jsonify({"ok": True, "changed": changed})

    # -----------------------
    # JSON API: Groups
    # -----------------------
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
        _broadcast("refresh", {"what": ["groups"]})
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
        _broadcast("refresh", {"what": ["groups"]})
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
            for d in gc_devicelist:
                if int(getattr(d, "groupId", -1)) == gid:
                    return jsonify({"ok": False, "error": "group not empty"}), 400
            del gc_grouplist[gid]
            try:
                gc_instance.save_to_db({"manual": True})
            except Exception:
                pass
        _broadcast("refresh", {"what": ["groups"]})
        return jsonify({"ok": True})

    @bp.route("/gatecontrol/api/groups/force", methods=["POST"])
    def api_groups_force():
        if _task_is_running():
            return _task_busy_response()
        try:
            gc_instance.forceGroups(args=None, sanityCheck=True)
        except Exception as ex:
            _log(f"GateControl: forceGroups failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500
        _broadcast("refresh", {"what": ["groups", "devices"]})
        return jsonify({"ok": True})

    # -----------------------
    # JSON API: Save/Reload
    # -----------------------
    @bp.route("/gatecontrol/api/save", methods=["POST"])
    def api_save():
        if _task_is_running():
            return _task_busy_response()
        try:
            gc_instance.save_to_db({"manual": True})
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        return jsonify({"ok": True})

    @bp.route("/gatecontrol/api/reload", methods=["POST"])
    def api_reload():
        if _task_is_running():
            return _task_busy_response()
        try:
            gc_instance.load_from_db()
        except Exception as ex:
            return jsonify({"ok": False, "error": str(ex)}), 500
        _broadcast("refresh", {"what": ["groups", "devices"]})
        return jsonify({"ok": True})

    # -----------------------
    # JSON API: CONTROL (flags/presetId)
    # -----------------------

    @bp.route("/gatecontrol/api/config", methods=["POST"])
    def api_config():
        """Send unicast CONFIG packet to exactly one node (no broadcast)."""
        if _task_is_running():
            return _task_busy_response()

        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        mac = body.get("mac", None)
        if mac and not macs:
            macs = [mac]

        if len(macs) != 1:
            return jsonify({"ok": False, "error": "select exactly one device"}), 400

        def _parse_recv3_from_addr(addr_str) -> Optional[bytes]:
            """Parse address string (3B or 6B, with/without separators) and return last3 bytes."""
            if addr_str is None:
                return None
            try:
                s = str(addr_str)
            except Exception:
                return None
            hexchars = "0123456789abcdefABCDEF"
            s = "".join(ch for ch in s if ch in hexchars)
            if len(s) < 6:
                return None
            s = s[-6:]
            try:
                return bytes.fromhex(s)
            except Exception:
                return None

        recv3 = _parse_recv3_from_addr(macs[0])
        if not recv3:
            return jsonify({"ok": False, "error": "invalid mac/address"}), 400
        if recv3 == b"\xFF\xFF\xFF":
            return jsonify({"ok": False, "error": "broadcast not allowed for config"}), 400

        try:
            option = int(body.get("option", 0)) & 0xFF
            flags  = int(body.get("flags", 0)) & 0xFF
        except Exception:
            return jsonify({"ok": False, "error": "invalid option/flags"}), 400

        if option not in (0x01, 0x02, 0x03, 0x04, 0x05):
            return jsonify({"ok": False, "error": "unknown config option"}), 400

        try:
            # Prefer instance helper if present
            if hasattr(gc_instance, "sendConfig"):
                gc_instance.sendConfig(option=option, flags=flags, recv3=recv3)
            else:
                gc_instance.lora.send_config(recv3=recv3, option=option, flags=flags)
        except Exception as ex:
            _log(f"GateControl: config failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500

        _set_master(state="TX", tx_pending=True, last_event="CONFIG_SENT")
        return jsonify({"ok": True, "sent": 1, "recv3": recv3.hex().upper(), "option": option, "flags": flags})

    @bp.route("/gatecontrol/api/devices/control", methods=["POST"])
    def api_devices_control():
        """
        CONTROL message:
          - per-device: {macs:[...], flags:int, presetId:int, brightness:int}
          - per-group:  {groupId:int, flags:int, presetId:int, brightness:int}
        """
        if _task_is_running():
            return _task_busy_response()

        body = request.get_json(silent=True) or {}
        macs = body.get("macs") or []
        group_id = body.get("groupId", None)

        flags = body.get("flags", None)
        presetId = body.get("presetId", None)
        brightness = body.get("brightness", None)

        def _toint(x, default=None):
            try:
                return int(x)
            except Exception:
                return default

        flags = _toint(flags, None)
        presetId = _toint(presetId, None)
        brightness = _toint(brightness, None)

        if flags is None or presetId is None or brightness is None:
            return jsonify({"ok": False, "error": "missing flags/presetId/brightness"}), 400

        changed = 0
        try:
            if group_id is not None:
                # Prefer new signature sendGroupControl(groupId, flags, presetId, brightness)
                try:
                    gc_instance.sendGroupControl(int(group_id), flags, presetId, brightness)
                except TypeError:
                    # fallback old signature if user hasn't applied proto patch (state/effect)
                    gc_instance.sendGroupControl(int(group_id), int(bool(flags & 0x01)), presetId, brightness)
                changed = 1
            elif macs:
                for mac in macs:
                    dev = gc_instance.getDeviceFromAddress(mac)
                    if dev:
                        try:
                            gc_instance.sendGateControl(dev, flags, presetId, brightness)
                        except TypeError:
                            gc_instance.sendGateControl(dev, int(bool(flags & 0x01)), presetId, brightness)
                        changed += 1
            else:
                return jsonify({"ok": False, "error": "missing macs or groupId"}), 400
        except Exception as ex:
            _log(f"GateControl: control failed: {ex}")
            return jsonify({"ok": False, "error": str(ex)}), 500

        _set_master(state="TX", tx_pending=True, last_event="CONTROL_SENT")
        return jsonify({"ok": True, "changed": changed})

    # Finally register blueprint
    rhapi.ui.blueprint_add(bp)
    _log("GateControl UI blueprint registered at /gatecontrol")
