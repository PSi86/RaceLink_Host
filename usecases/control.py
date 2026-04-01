from __future__ import annotations

import logging
from datetime import datetime

from ..data import RL_DeviceGroup, RL_FLAG_HAS_BRI, RL_FLAG_POWER_ON, get_specials_config

logger = logging.getLogger(__name__)


def _to_int(value, fallback=0) -> int:
    try:
        return int(value)
    except Exception:
        return int(fallback)


def _build_flags(brightness: int) -> int:
    return (RL_FLAG_POWER_ON if int(brightness) > 0 else 0) | RL_FLAG_HAS_BRI


def apply_device_control(service, *, target_device, brightness: int, effect: int) -> dict:
    if target_device is None:
        return {"ok": False, "message": "Device not found"}

    target_brightness = _to_int(brightness)
    target_effect = _to_int(effect)
    target_flags = _build_flags(target_brightness)

    service.sendRaceLink(target_device, target_flags, target_effect, target_brightness)
    return {
        "ok": True,
        "message": f"Device control applied ({getattr(target_device, 'name', 'unknown')})",
    }


def apply_group_control(service, *, group_id: int, brightness: int, effect: int) -> dict:
    target_group = _to_int(group_id)
    target_brightness = _to_int(brightness)
    target_effect = _to_int(effect)
    target_flags = _build_flags(target_brightness)

    service.sendGroupControl(target_group, target_flags, target_effect, target_brightness)
    return {"ok": True, "message": f"Group control applied (group {target_group})"}


def run_discovery(service, *, selected_group: int, new_group_name: str | None) -> dict:
    group_selected = _to_int(selected_group)
    new_group_str = (new_group_name or "").strip()

    created_group = False
    groups = service.get_groups()
    if group_selected == 0:
        if not new_group_str:
            new_group_str = "New Group"
        new_group_str = f"{new_group_str} {datetime.now().strftime('%Y%m%d_%H%M%S')}"
        group_selected = len(groups)

    num_found = service.getDevices(groupFilter=0, addToGroup=group_selected)

    if num_found > 0 and group_selected == len(groups):
        groups.append(RL_DeviceGroup(new_group_str))
        created_group = True

    return {
        "ok": True,
        "num_found": int(num_found),
        "group_created": created_group,
        "group_name": new_group_str if created_group else None,
    }


def execute_special_action(service, *, action: dict, fn_key: str, mode: str) -> dict:
    specials = get_specials_config()
    fn_info = None
    cap_key = None
    for cap, info in specials.items():
        for fn in info.get("functions", []) or []:
            if fn.get("key") == fn_key:
                fn_info = fn
                cap_key = cap
                break
        if fn_info:
            break

    if not fn_info:
        return {"ok": False, "message": f"Special function not found: {fn_key}"}

    vars_list = fn_info.get("vars", []) or []
    params = {var: _to_int(action.get(f"rl_special_{fn_key}_{var}"), action.get(f"rl_special_{fn_key}_{var}")) for var in vars_list}

    target_device = None
    target_group = None
    if mode == "device":
        target_addr = action.get(f"rl_special_{fn_key}_device")
        if target_addr:
            target_device = service.getDeviceFromAddress(target_addr)
        if target_device is None:
            return {"ok": False, "message": "Target device not found"}
    else:
        target_group = _to_int(action.get(f"rl_special_{fn_key}_group"), -1)
        if target_group < 0:
            return {"ok": False, "message": "Invalid group target"}

    comm_name = fn_info.get("comm")
    if not comm_name:
        return {"ok": False, "message": f"Missing comm function for {fn_key}"}

    comm_fn = getattr(service, comm_name, None)
    if not callable(comm_fn):
        return {"ok": False, "message": f"Comm function unavailable: {comm_name}"}

    logger.debug("RL usecase: special action %s (%s)", fn_key, cap_key or "unknown")
    comm_fn(targetDevice=target_device, targetGroup=target_group, params=params)
    return {"ok": True, "message": f"Special action executed: {fn_key}"}
