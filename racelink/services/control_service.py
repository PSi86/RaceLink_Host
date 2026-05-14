"""Control message service for active device/group operations.

Method naming mirrors the protocol opcodes:

- ``send_wled_preset`` sends a WLED preset id (``OPC_PRESET``, 4 B fixed
  body). The preset must already exist in the device's active
  ``presets.json``.
- ``send_control`` sends a direct effect-parameter packet (``OPC_CONTROL``,
  variable-length body). The opcode and this method are protocol-level —
  not WLED-specific — so future lighting backends can re-use the same
  packet shape.
- ``send_rl_preset_by_id`` resolves a stable RL-preset id to its
  persisted 14-field parameter snapshot, then dispatches through
  ``send_control``.
"""

from __future__ import annotations

import logging

from ..domain import USER_FLAG_KEYS, build_flags_byte
from ..transport import mac_last3_from_hex

logger = logging.getLogger(__name__)


class ControlService:
    def __init__(self, controller, gateway_service):
        self.controller = controller
        self.gateway_service = gateway_service

    @property
    def transport(self):
        return getattr(self.controller, "transport", None)

    def _require_transport(self, context: str):
        """Return the active transport or ``None`` after logging a warning.

        Callers should bind the return value to a local variable and guard on
        it so both the runtime check and the static type-narrowing apply at
        the same site.
        """
        transport = self.transport
        if transport is None:
            logger.warning("%s: communicator not ready", context)
            return None
        return transport

    @staticmethod
    def _coerce_preset_values(flags, preset_id, brightness, *, fallback=None):
        if fallback is not None:
            flags = fallback.flags if flags is None else flags
            preset_id = fallback.presetId if preset_id is None else preset_id
            brightness = fallback.brightness if brightness is None else brightness
        return int(flags) & 0xFF, int(preset_id) & 0xFF, int(brightness) & 0xFF

    def _update_group_preset_cache(self, group_id: int, flags: int, preset_id: int, brightness: int) -> None:
        for device in self.controller.device_repository.list():
            try:
                if (int(getattr(device, "groupId", 0)) & 0xFF) != group_id:
                    continue
                device.flags = flags
                device.presetId = preset_id
                device.brightness = brightness
            except Exception:
                # swallow-ok: bulk cache update keeps going on the
                # remaining devices. Per-device failure here means
                # malformed groupId / non-int field — a data quality
                # issue worth diagnosing, so debug-log with traceback
                # rather than silently dropping (B5).
                logger.debug(
                    "group-preset cache update skipped device %r",
                    getattr(device, "addr", "?"),
                    exc_info=True,
                )
                continue

    def _apply_control_locally(self, dev, *, flags: int, params: dict, brightness_val: int, brightness_present: bool) -> None:
        """Mirror what we just told the device onto the local DTO so the
        device-table renders the new state immediately.

        Iteration 9: ``OPC_CONTROL`` is RESP_NONE on the wire (per
        ``racelink_proto_auto.py::RULES``); per the operator's principle
        — RESP_NONE control ops update local state immediately
        (optimistic) — we mirror the just-sent fields here. The
        symmetric eager-update for ``OPC_PRESET`` lives in
        ``send_device_preset`` / ``_update_group_preset_cache``.

        ``presetId`` is intentionally NOT touched — it's the host's
        "last preset id I asked the device to apply" tracker. The
        ``send_rl_preset_by_id`` path stamps it separately with the
        applied RL-preset id; raw ``send_control`` callers (e.g. scene
        ``rl_effect`` actions) shouldn't overwrite that.
        """
        try:
            dev.flags = int(flags) & 0xFF
            mode = params.get("mode")
            if mode is not None:
                dev.effectId = int(mode) & 0xFF
            # Brightness only mirrors when the wire frame actually
            # carried a brightness byte (HAS_BRI). Otherwise the prior
            # device brightness stays — matches the "absent fields keep
            # their previous value" semantic of OPC_CONTROL's fieldMask.
            if brightness_present:
                dev.brightness = int(brightness_val) & 0xFF
        except Exception:
            # swallow-ok: best-effort cache mirror — wire send already
            # succeeded, this just keeps the UI in sync. A failure here
            # (malformed dev field) is worth a debug log so a recurring
            # data-quality issue surfaces.
            logger.debug(
                "_apply_control_locally skipped on dev %r",
                getattr(dev, "addr", "?"),
                exc_info=True,
            )

    def _update_group_control_cache(self, group_id: int, *, flags: int, params: dict, brightness_val: int, brightness_present: bool) -> None:
        for device in self.controller.device_repository.list():
            try:
                if (int(getattr(device, "groupId", 0)) & 0xFF) != group_id:
                    continue
            except Exception:
                logger.debug(
                    "group-control cache filter skipped device %r",
                    getattr(device, "addr", "?"),
                    exc_info=True,
                )
                continue
            self._apply_control_locally(
                device,
                flags=flags,
                params=params,
                brightness_val=brightness_val,
                brightness_present=brightness_present,
            )

    def send_device_preset(self, target_device, flags=None, preset_id=None, brightness=None) -> bool:
        """Send OPC_PRESET to a single node (receiver = last3 of targetDevice.addr).

        Returns ``True`` if a frame was queued for the gateway, ``False``
        if the transport is not ready. Pre-B2 fix this fell off the end
        as ``None``, which the wrapper :meth:`send_wled_preset`
        misinterpreted as success — operators saw 200 OK with no packet
        on the wire.
        """
        transport = self._require_transport("sendDevicePreset")
        if transport is None:
            return False

        recv3 = mac_last3_from_hex(target_device.addr)
        group_id = int(target_device.groupId) & 0xFF
        flags_b, preset_b, brightness_b = self._coerce_preset_values(
            flags,
            preset_id,
            brightness,
            fallback=target_device,
        )

        transport.send_preset(
            recv3=recv3,
            group_id=group_id,
            flags=flags_b,
            preset_id=preset_b,
            brightness=brightness_b,
        )

        target_device.flags = flags_b
        target_device.presetId = preset_b
        target_device.brightness = brightness_b
        logger.debug(
            "RL: Updated Device %s: flags=0x%02X presetId=%d brightness=%d",
            target_device.addr,
            target_device.flags,
            target_device.presetId,
            target_device.brightness,
        )
        return True

    def send_group_preset(self, group_id, flags, preset_id, brightness) -> bool:
        """Broadcast OPC_PRESET to a group; update local cache for matching devices.

        Returns ``True`` if a frame was queued, ``False`` when the
        transport is not ready. Same B2 contract as
        :meth:`send_device_preset`.
        """
        transport = self._require_transport("sendGroupPreset")
        if transport is None:
            return False

        group_b = int(group_id) & 0xFF
        flags_b, preset_b, brightness_b = self._coerce_preset_values(flags, preset_id, brightness)
        self._update_group_preset_cache(group_b, flags_b, preset_b, brightness_b)

        transport.send_preset(
            recv3=b"\xFF\xFF\xFF",
            group_id=group_b,
            flags=flags_b,
            preset_id=preset_b,
            brightness=brightness_b,
        )
        return True

    def send_wled_preset(self, *, targetDevice=None, targetGroup=None, params=None):
        """Apply a classical WLED preset (OPC_PRESET) to a device or group.

        Accepts numeric ``presetId`` only. RaceLink-native RL-presets follow a
        separate path via :meth:`send_rl_preset_by_id`.

        Flag handling is identical to :meth:`send_control` (protocol
        contract, ``racelink_proto.h`` byte 1 on both opcodes). Honours the
        four user-intent flags from ``params``: ``arm_on_sync``, ``force_tt0``,
        ``force_reapply``, ``offset_mode``. ``POWER_ON``/``HAS_BRI`` are
        derived from brightness and never passed through from the caller.
        """
        if params is None:
            params = {}
        preset_id = int(params.get("presetId", 1))
        brightness = int(params.get("brightness", 0))
        flags = build_flags_byte(
            power_on=brightness > 0,
            has_bri=True,
            arm_on_sync=bool(params.get("arm_on_sync")),
            force_tt0=bool(params.get("force_tt0")),
            force_reapply=bool(params.get("force_reapply")),
            offset_mode=bool(params.get("offset_mode")),
        )

        # B2: propagate the underlying boolean — ``send_group_preset`` /
        # ``send_device_preset`` return False when the transport is
        # missing. Pre-fix this wrapper unconditionally returned True,
        # so a route or scene-runner caller saw success even though no
        # frame went out.
        if targetGroup is not None:
            return self.send_group_preset(int(targetGroup), flags, preset_id, brightness)
        if targetDevice is not None:
            return self.send_device_preset(targetDevice, flags, preset_id, brightness)
        return False

    def send_offset(self, *, targetDevice=None, targetGroup=None,
                    mode="none", **mode_params) -> bool:
        """Send OPC_OFFSET (variable-length, 2..7 B body).

        ``mode`` selects the formula stored on the receiver and accepts
        either the int enum value (``OFFSET_MODE_LINEAR``) or its lowercase
        name (``"linear"``). Per-mode kwargs:

            "none":     (no extra args; clears stored config)
            "explicit": offset_ms
            "linear":   base_ms, step_ms
            "vshape":   base_ms, step_ms, center
            "modulo":   base_ms, step_ms, cycle

        With ``targetGroup=255`` the body's groupId is the broadcast sentinel
        — every device picks it up. The acceptance gate on subsequent
        OPC_CONTROL packets selects the participating subset.

        Returns ``True`` if a frame was queued, ``False`` if the transport is
        not ready or no target was provided.
        """
        transport = self._require_transport("sendOffset")
        if transport is None:
            return False

        if targetGroup is not None:
            group_b = int(targetGroup) & 0xFF
            transport.send_offset(
                recv3=b"\xFF\xFF\xFF", group_id=group_b,
                mode=mode, **mode_params,
            )
            return True
        if targetDevice is not None:
            recv3 = mac_last3_from_hex(targetDevice.addr)
            group_b = int(getattr(targetDevice, "groupId", 0)) & 0xFF
            transport.send_offset(
                recv3=recv3, group_id=group_b,
                mode=mode, **mode_params,
            )
            return True
        return False

    def send_rl_preset_by_id(
        self,
        preset_id,
        *,
        targetDevice=None,
        targetGroup=None,
        brightness_override=None,
    ):
        """Apply a RaceLink-native preset (OPC_CONTROL) by its stable int id.

        Loads the persisted parameter snapshot via ``rl_presets_service`` and
        delegates to :meth:`send_control` with the merged params.
        ``brightness_override`` (e.g. from a RotorHazard Quickset slider) takes
        precedence over the value saved in the preset. Flags stored with the
        preset (``arm_on_sync`` / ``force_tt0`` / ``force_reapply``) are
        forwarded as ``params[...]`` toggles so the direct sender picks them
        up via its existing flag-derivation logic.

        Returns ``True`` on success, ``False`` on unknown id / missing service.
        """
        rl_service = getattr(self.controller, "rl_presets_service", None)
        if rl_service is None:
            logger.warning("sendRlPresetById: rl_presets_service not wired")
            return False
        try:
            pid = int(preset_id)
        except (TypeError, ValueError):
            logger.warning("sendRlPresetById: invalid preset id %r", preset_id)
            return False

        preset = rl_service.get_by_id(pid)
        if preset is None:
            logger.warning("sendRlPresetById: preset id=%d not found", pid)
            return False

        merged = dict(preset.get("params") or {})
        if brightness_override is not None:
            merged["brightness"] = int(brightness_override)

        flags_meta = preset.get("flags") or {}
        for key in USER_FLAG_KEYS:
            if flags_meta.get(key):
                merged[key] = True

        ok = self.send_control(
            targetDevice=targetDevice,
            targetGroup=targetGroup,
            params=merged,
        )

        # Iteration 8: track the just-applied RL-preset id on the
        # targeted device so the Device Options dialog's "RaceLink
        # Preset" dropdown can pre-select it on next open. Without
        # this, ``dev.presetId`` stayed at whatever ``send_device_preset``
        # last wrote (a WLED preset id, possibly 0) and the dropdown
        # rendered with no selection. Mirror the same write to every
        # device in a target group so a fleet-apply also sticks.
        if ok:
            if targetDevice is not None:
                try:
                    targetDevice.presetId = pid & 0xFF
                except Exception:
                    # swallow-ok: best-effort cache update; the wire send
                    # already succeeded, the dropdown just won't pre-fill.
                    logger.debug(
                        "send_rl_preset_by_id: could not stamp presetId on %r",
                        getattr(targetDevice, "addr", "?"),
                        exc_info=True,
                    )
            elif targetGroup is not None:
                gid = int(targetGroup) & 0xFF
                for dev in self.controller.device_repository.list():
                    try:
                        if (int(getattr(dev, "groupId", 0)) & 0xFF) == gid:
                            dev.presetId = pid & 0xFF
                    except Exception:
                        logger.debug(
                            "send_rl_preset_by_id: group cache update skipped %r",
                            getattr(dev, "addr", "?"),
                            exc_info=True,
                        )
        return ok

    def send_control(self, *, targetDevice=None, targetGroup=None, params=None):
        """Send OPC_CONTROL with the effect parameters from ``params``.

        Full-state semantics: every field present in ``params`` is included in
        the serialized body. Fields set to ``None`` (or missing entirely) are
        omitted via the ``fieldMask``/``extMask`` presence bits, so the
        receiver leaves them untouched.

        Flag handling matches :meth:`send_wled_preset`: ``POWER_ON`` derived
        from brightness, ``HAS_BRI`` always set when brightness is provided.
        """
        transport = self._require_transport("sendControl")
        if transport is None:
            return False

        params = params or {}
        brightness = params.get("brightness")
        brightness_val = int(brightness) & 0xFF if brightness is not None else 0
        # Flag byte is identical for OPC_PRESET and OPC_CONTROL by protocol
        # contract (see comment in ``racelink_proto.h``). Composed via the
        # shared ``build_flags_byte`` helper.
        flags = build_flags_byte(
            power_on=brightness_val > 0,
            has_bri=True,
            arm_on_sync=bool(params.get("arm_on_sync")),
            force_tt0=bool(params.get("force_tt0")),
            force_reapply=bool(params.get("force_reapply")),
            offset_mode=bool(params.get("offset_mode")),
        )

        # Collect the kwargs for build_control_body(). Only pass fields
        # actually present in params so the builder emits a minimal body (any
        # unsupplied field stays out of fieldMask/extMask).
        ctrl_kwargs: dict = {}
        if brightness is not None:
            ctrl_kwargs["brightness"] = brightness_val
        for key in ("mode", "speed", "intensity", "custom1", "custom2", "custom3", "palette"):
            if key in params and params[key] is not None:
                ctrl_kwargs[key] = int(params[key]) & 0xFF
        for key in ("check1", "check2", "check3"):
            if key in params and params[key] is not None:
                ctrl_kwargs[key] = bool(params[key])
        for key in ("color1", "color2", "color3"):
            if key in params and params[key] is not None:
                ctrl_kwargs[key] = tuple(int(c) & 0xFF for c in params[key])

        brightness_present = brightness is not None

        if targetGroup is not None:
            group_b = int(targetGroup) & 0xFF
            transport.send_control(
                recv3=b"\xFF\xFF\xFF",
                group_id=group_b,
                flags=flags,
                **ctrl_kwargs,
            )
            # Iteration 9: OPC_CONTROL is RESP_NONE — mirror onto every
            # group member's DTO so the device-table reflects the new
            # state immediately (no need to wait for a manual Get
            # Status). The route handler's SSE refresh propagates the
            # change to the frontend.
            self._update_group_control_cache(
                group_b,
                flags=flags,
                params=params,
                brightness_val=brightness_val,
                brightness_present=brightness_present,
            )
            return True
        if targetDevice is not None:
            recv3 = mac_last3_from_hex(targetDevice.addr)
            group_b = int(getattr(targetDevice, "groupId", 0)) & 0xFF
            transport.send_control(
                recv3=recv3,
                group_id=group_b,
                flags=flags,
                **ctrl_kwargs,
            )
            # Iteration 9: see _update_group_control_cache comment above —
            # symmetric optimistic mirror for the unicast path.
            self._apply_control_locally(
                targetDevice,
                flags=flags,
                params=params,
                brightness_val=brightness_val,
                brightness_present=brightness_present,
            )
            logger.debug(
                "RL: Sent CONTROL to %s: flags=0x%02X fields=%s",
                targetDevice.addr,
                flags,
                sorted(ctrl_kwargs.keys()),
            )
            return True
        return False
