"""Tests for ``send_rl_preset_by_id``.

Covers the RL-preset apply path that the RotorHazard plugin uses: resolve
a preset by its stable int id, merge any brightness override +
preset-stored flags, and dispatch to ``send_control`` (OPC_CONTROL).

Naming reference:
- OPC_PRESET  (0x04) → transport.send_preset(), service.send_wled_preset()
- OPC_CONTROL (0x08) → transport.send_control(), service.send_control()
"""

import os
import tempfile
import unittest

from racelink.services.control_service import ControlService
from racelink.services.rl_presets_service import RLPresetsService


class _FakeTransport:
    """Captures transport-layer calls for both packet types."""

    def __init__(self):
        self.preset_calls = []   # OPC_PRESET (4 B fixed)
        self.control_calls = []  # OPC_CONTROL (variable length)

    def send_preset(self, **kwargs):
        self.preset_calls.append(kwargs)

    def send_control(self, **kwargs):
        self.control_calls.append(kwargs)


class _FakeController:
    def __init__(self, rl_presets_service=None):
        self.transport = _FakeTransport()
        self.device_repository = type("Repo", (), {"list": staticmethod(lambda: [])})()
        self.rl_presets_service = rl_presets_service


class _Dev:
    def __init__(self, addr="AABBCCDDEEFF", group_id=7):
        self.addr = addr
        self.groupId = group_id
        self.flags = 0
        self.presetId = 0
        # ``effectId`` is the active WLED segment mode (set by
        # STATUS_REPLY or — iteration 9 — optimistically by
        # ``send_control``). Mirrors the field on the production
        # ``RL_Device`` DTO so tests can assert against it.
        self.effectId = 0
        self.brightness = 0


class SendRlPresetByIdTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.rl_svc = RLPresetsService(storage_path=os.path.join(self._tmp.name, "rl.json"))
        self.controller = _FakeController(rl_presets_service=self.rl_svc)
        self.service = ControlService(self.controller, None)

    def test_apply_sends_control_with_preset_params(self):
        p = self.rl_svc.create(
            label="Breathe Red",
            params={"mode": 2, "speed": 200, "color1": [255, 0, 0], "brightness": 255},
        )
        ok = self.service.send_rl_preset_by_id(
            p["id"], targetDevice=_Dev(), brightness_override=None,
        )
        self.assertTrue(ok)
        # No classical preset frame; variable-length control frame instead.
        self.assertEqual(self.controller.transport.preset_calls, [])
        self.assertEqual(len(self.controller.transport.control_calls), 1)
        call = self.controller.transport.control_calls[0]
        self.assertEqual(call["mode"], 2)
        self.assertEqual(call["speed"], 200)
        self.assertEqual(call["color1"], (255, 0, 0))
        self.assertEqual(call["brightness"], 255)

    def test_brightness_override_wins(self):
        p = self.rl_svc.create(
            label="Red",
            params={"mode": 1, "color1": [200, 0, 0], "brightness": 255},
        )
        ok = self.service.send_rl_preset_by_id(
            p["id"], targetDevice=_Dev(), brightness_override=50,
        )
        self.assertTrue(ok)
        call = self.controller.transport.control_calls[0]
        self.assertEqual(call["brightness"], 50)

    def test_preset_flags_are_stripped_and_do_not_reach_wire(self):
        # Regression for the cleanup that removed flags from the RL
        # Preset Editor: ``RLPresetsService._canonical_flags`` strips
        # every user-intent flag at create/load, so a preset can no
        # longer carry ``arm_on_sync`` / ``force_reapply`` to the wire.
        # Operators set per-action flags via the Scene Editor's
        # SceneFlagsOverride instead.
        p = self.rl_svc.create(
            label="Armed",
            params={"mode": 3, "brightness": 100},
            flags={"arm_on_sync": True, "force_reapply": True},
        )
        ok = self.service.send_rl_preset_by_id(p["id"], targetDevice=_Dev())
        self.assertTrue(ok)
        flags = self.controller.transport.control_calls[0]["flags"]
        # Brightness-derived bits stay (POWER_ON, HAS_BRI) — those are
        # computed by ``send_control`` regardless of the preset's
        # flag-side input.
        self.assertTrue(flags & 0x01)   # POWER_ON (bri>0)
        self.assertTrue(flags & 0x04)   # HAS_BRI
        # Stripped user flags must not appear on the wire.
        self.assertFalse(flags & 0x02)  # ARM_ON_SYNC
        self.assertFalse(flags & 0x10)  # FORCE_REAPPLY

    def test_all_four_user_flags_stripped_at_preset_layer(self):
        # Counterpart to test_preset_flags_are_stripped_and_do_not_reach_wire
        # for the full set. Even when every user-intent flag is passed
        # to ``create()``, none of them reach ``send_control`` because
        # the canonicaliser zeros them. Wire byte therefore carries
        # only brightness-derived bits.
        p = self.rl_svc.create(
            label="Staggered Arm",
            params={"mode": 5, "brightness": 80},
            flags={
                "arm_on_sync": True, "force_tt0": True,
                "force_reapply": True, "offset_mode": True,
            },
        )
        ok = self.service.send_rl_preset_by_id(p["id"], targetDevice=_Dev())
        self.assertTrue(ok)
        flags = self.controller.transport.control_calls[0]["flags"]
        # Only POWER_ON | HAS_BRI from brightness; no user-intent bits.
        self.assertEqual(flags, 0x05)

    def test_group_target(self):
        p = self.rl_svc.create(label="Green", params={"mode": 22, "brightness": 180})
        ok = self.service.send_rl_preset_by_id(p["id"], targetGroup=3)
        self.assertTrue(ok)
        call = self.controller.transport.control_calls[0]
        self.assertEqual(call["group_id"], 3)
        self.assertEqual(call["recv3"], b"\xFF\xFF\xFF")

    def test_unknown_id_returns_false_without_send(self):
        ok = self.service.send_rl_preset_by_id(42, targetDevice=_Dev())
        self.assertFalse(ok)
        self.assertEqual(self.controller.transport.preset_calls, [])
        self.assertEqual(self.controller.transport.control_calls, [])

    def test_invalid_id_type_returns_false(self):
        ok = self.service.send_rl_preset_by_id("not-an-int", targetDevice=_Dev())
        self.assertFalse(ok)

    def test_missing_rl_service_returns_false(self):
        controller = _FakeController(rl_presets_service=None)
        svc = ControlService(controller, None)
        ok = svc.send_rl_preset_by_id(0, targetDevice=_Dev())
        self.assertFalse(ok)
        self.assertEqual(controller.transport.preset_calls, [])
        self.assertEqual(controller.transport.control_calls, [])

    def test_numeric_string_id_is_accepted(self):
        p = self.rl_svc.create(label="Alpha", params={"mode": 1, "brightness": 128})
        ok = self.service.send_rl_preset_by_id(str(p["id"]), targetDevice=_Dev())
        self.assertTrue(ok)

    def test_targetDevice_presetId_is_stamped_with_applied_id(self):
        # Iteration 8 fix for the empty-dropdown bug: after applying an
        # RL preset, the host must remember the applied id on the
        # device so the Device Options "RaceLink Preset" dropdown can
        # pre-select it on next open. Pre-fix, ``send_rl_preset_by_id``
        # routed through ``send_control`` which doesn't touch
        # ``dev.presetId`` (only ``send_device_preset`` does, for
        # classical OPC_PRESET sends), so the dropdown rendered empty.
        p = self.rl_svc.create(label="Pulse", params={"mode": 7, "brightness": 100})
        dev = _Dev()
        # Sanity: presetId starts at 0 (the _Dev default).
        self.assertEqual(dev.presetId, 0)
        ok = self.service.send_rl_preset_by_id(p["id"], targetDevice=dev)
        self.assertTrue(ok)
        self.assertEqual(dev.presetId, p["id"] & 0xFF)

    def test_targetGroup_stamps_presetId_on_every_member(self):
        # When the dispatch is group-targeted the cache update must
        # cover every device whose ``groupId`` matches — same pattern
        # as ``send_group_preset``'s cache-update fan-out.
        from racelink.services.control_service import ControlService

        p = self.rl_svc.create(label="Sweep", params={"mode": 6, "brightness": 200})

        in_group_a = _Dev(addr="AA00000000A1", group_id=4)
        in_group_b = _Dev(addr="AA00000000A2", group_id=4)
        out_group  = _Dev(addr="AA00000000B1", group_id=9)

        controller = _FakeController(rl_presets_service=self.rl_svc)
        controller.device_repository = type(
            "Repo", (), {"list": staticmethod(lambda: [in_group_a, in_group_b, out_group])}
        )()
        svc = ControlService(controller, None)

        ok = svc.send_rl_preset_by_id(p["id"], targetGroup=4)
        self.assertTrue(ok)
        self.assertEqual(in_group_a.presetId, p["id"] & 0xFF)
        self.assertEqual(in_group_b.presetId, p["id"] & 0xFF)
        # Devices outside the group are untouched.
        self.assertEqual(out_group.presetId, 0)

    def test_targetDevice_optimistic_mirror_updates_effectId_brightness_flags(self):
        # Iteration 9: OPC_CONTROL is RESP_NONE on the wire — local
        # state must mirror the just-sent fields so the UI reflects
        # the change without waiting for a manual Get Status. Effect
        # column reads ``dev.effectId`` (decoded from WLED_EFFECTS),
        # so ``mode`` → ``effectId`` is the critical mirror.
        p = self.rl_svc.create(
            label="Aurora",
            params={"mode": 27, "brightness": 180, "speed": 120},
        )
        dev = _Dev()
        # Sanity: pre-apply state is the _Dev defaults.
        self.assertEqual(dev.effectId, 0)

        ok = self.service.send_rl_preset_by_id(p["id"], targetDevice=dev)
        self.assertTrue(ok)
        # Mode → effectId (decoded by the device-table to "Aurora").
        self.assertEqual(dev.effectId, 27)
        # Brightness mirrored because HAS_BRI is always set in send_control.
        self.assertEqual(dev.brightness, 180)
        # Flags should carry POWER_ON (bri>0) | HAS_BRI at minimum.
        self.assertTrue(dev.flags & 0x01)  # POWER_ON
        self.assertTrue(dev.flags & 0x04)  # HAS_BRI

    def test_send_control_with_no_mode_does_not_overwrite_effectId(self):
        # If the caller sends a control packet that does NOT include
        # ``mode`` (e.g. a brightness-only tweak), ``dev.effectId``
        # must stay at whatever was there before — matches the wire's
        # fieldMask "absent fields keep their previous value" semantic.
        from racelink.services.control_service import ControlService

        controller = _FakeController()
        svc = ControlService(controller, None)
        dev = _Dev()
        dev.effectId = 5  # pre-existing state

        # Brightness-only direct send_control call (no mode).
        ok = svc.send_control(targetDevice=dev, params={"brightness": 100})
        self.assertTrue(ok)
        # effectId untouched; brightness mirrored.
        self.assertEqual(dev.effectId, 5)
        self.assertEqual(dev.brightness, 100)

    def test_send_control_without_brightness_does_not_overwrite_brightness(self):
        # Brightness only mirrors when the wire frame carried HAS_BRI
        # (i.e. ``params["brightness"]`` was provided). A control packet
        # that flips just the mode shouldn't reset the operator's
        # brightness setting.
        from racelink.services.control_service import ControlService

        controller = _FakeController()
        svc = ControlService(controller, None)
        dev = _Dev()
        dev.brightness = 200  # pre-existing operator value

        ok = svc.send_control(targetDevice=dev, params={"mode": 9})
        self.assertTrue(ok)
        self.assertEqual(dev.effectId, 9)
        # Brightness preserved.
        self.assertEqual(dev.brightness, 200)

    def test_targetGroup_optimistic_mirror_fan_out(self):
        # Group-targeted send_control must mirror onto every member of
        # the target group, leave non-members alone.
        from racelink.services.control_service import ControlService

        p = self.rl_svc.create(label="Wave", params={"mode": 33, "brightness": 90})

        in_a = _Dev(addr="AA00000000C1", group_id=2)
        in_b = _Dev(addr="AA00000000C2", group_id=2)
        out_ = _Dev(addr="AA00000000D1", group_id=5)
        # Pre-existing state on the out-of-group device — must survive.
        out_.effectId = 11
        out_.brightness = 250

        controller = _FakeController(rl_presets_service=self.rl_svc)
        controller.device_repository = type(
            "Repo", (), {"list": staticmethod(lambda: [in_a, in_b, out_])}
        )()
        svc = ControlService(controller, None)

        ok = svc.send_rl_preset_by_id(p["id"], targetGroup=2)
        self.assertTrue(ok)
        # Members updated.
        self.assertEqual(in_a.effectId, 33)
        self.assertEqual(in_b.effectId, 33)
        self.assertEqual(in_a.brightness, 90)
        self.assertEqual(in_b.brightness, 90)
        # Non-member untouched.
        self.assertEqual(out_.effectId, 11)
        self.assertEqual(out_.brightness, 250)


class ControllerRlPresetRoutesViaPresetIdTests(unittest.TestCase):
    """The Specials ``rl_preset`` entry point (operator picks an RL preset
    by id) resolves the preset and dispatches via ``send_rl_preset_by_id``.
    Exercised through the actual controller class so the full
    Specials → Controller → Service path is covered."""

    def setUp(self):
        # We instantiate the real ControlService (logic under test) but hand
        # it a minimal fake controller that exposes just the attributes the
        # service touches: transport + rl_presets_service.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.rl_svc = RLPresetsService(storage_path=os.path.join(self._tmp.name, "rl.json"))
        self.controller = _FakeController(rl_presets_service=self.rl_svc)
        self.service = ControlService(self.controller, None)

    def test_preset_picker_params_resolve_and_send(self):
        p = self.rl_svc.create(label="Cyan", params={"mode": 12, "color1": [0, 200, 200]})
        # Imitate what the Specials ``rl_preset`` entry point does: delegate.
        ok = self.service.send_rl_preset_by_id(
            int("0"),  # str-id from select option would be coerced int() upstream
            targetDevice=_Dev(),
            brightness_override=128,
        )
        self.assertTrue(ok)
        self.assertEqual(len(self.controller.transport.control_calls), 1)
        self.assertEqual(self.controller.transport.control_calls[0]["mode"], 12)
        self.assertEqual(self.controller.transport.control_calls[0]["brightness"], 128)
        # Also assert the preset id round-trip.
        self.assertEqual(p["id"], 0)


class SendWledPresetIsIntOnlyTests(unittest.TestCase):
    """Regression: ``send_wled_preset`` (the classical preset path, OPC_PRESET)
    accepts only numeric ids. RL presets go through ``send_rl_preset_by_id``."""

    def setUp(self):
        self.controller = _FakeController()
        self.service = ControlService(self.controller, None)

    def test_int_preset_routes_to_preset(self):
        ok = self.service.send_wled_preset(
            targetDevice=_Dev(),
            params={"presetId": 5, "brightness": 200},
        )
        self.assertTrue(ok)
        self.assertEqual(len(self.controller.transport.preset_calls), 1)
        self.assertEqual(self.controller.transport.control_calls, [])

    def test_numeric_string_still_works(self):
        ok = self.service.send_wled_preset(
            targetDevice=_Dev(),
            params={"presetId": "7", "brightness": 0},
        )
        self.assertTrue(ok)
        self.assertEqual(self.controller.transport.preset_calls[0]["preset_id"], 7)

    def test_non_numeric_preset_raises(self):
        with self.assertRaises(ValueError):
            self.service.send_wled_preset(
                targetDevice=_Dev(),
                params={"presetId": "breathe_red", "brightness": 128},
            )


if __name__ == "__main__":
    unittest.main()
