from __future__ import annotations

from typing import Optional

from flask import jsonify


def parse_recv3_from_addr(addr_str) -> Optional[bytes]:
    if addr_str is None:
        return None
    s = "".join(ch for ch in str(addr_str) if ch in "0123456789abcdefABCDEF")
    if len(s) < 6:
        return None
    try:
        return bytes.fromhex(s[-6:])
    except Exception:
        return None


def busy_task_response(task_snapshot):
    return jsonify({"ok": False, "busy": True, "task": task_snapshot}), 409
