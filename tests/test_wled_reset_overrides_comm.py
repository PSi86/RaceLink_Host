"""Controller-level test for sendWledResetOverrides (OPC_CONFIG 0x0F).

Iteration 5 added a "Reset to RaceLink defaults" action surfaced in
the Device Options dialog. The controller comm method:

* Sends OPC_CONFIG with option=0x0F to the device's last-3 MAC.
* Resets ``dev.specials[wled_*]`` to schema defaults on a successful
  ACK, leaving non-WLED keys (e.g. STARTBLOCK slots) untouched.
* Persists via ``DEVICE_SPECIALS`` scope.
* Refuses target_group (unicast-only) and non-WLED devices.
"""

from __future__ import annotations

import unittest

from tests._flask_stub import install_serial

install_serial()

from racelink.controller import RaceLink_Host  # noqa: E402  (after install_serial)
from racelink.domain import RL_Device, RL_Dev_Type, build_specials_state, state_scope  # noqa: E402


class _FakeController:
    """Minimal controller stand-in. Binds the production
    ``sendWledResetOverrides`` method onto the fake so we test the
    real implementation without spinning up the full RaceLink_Host."""

    def __init__(self):
        self.send_config_calls: list[dict] = []
        self.saved: list = []
        self._sendConfig_returns = True

    def sendConfig(self, **kwargs):
        self.send_config_calls.append(kwargs)
        return self._sendConfig_returns

    def save_to_db(self, args, scopes=None):
        self.saved.append((args, scopes))

    sendWledResetOverrides = RaceLink_Host.sendWledResetOverrides


def _make_wled(addr: str = "AABBCCDDEEFF", *, custom_overrides=None):
    dev = RL_Device(
        addr,
        RL_Dev_Type.NODE_WLED_REV5,
        "WLED",
        caps=RL_Dev_Type.NODE_WLED_REV5,
    )
    dev.specials = build_specials_state(RL_Dev_Type.NODE_WLED_REV5, stored=custom_overrides or {})
    return dev


def _make_startblock(addr: str = "001122334455", *, custom_overrides=None):
    dev = RL_Device(
        addr,
        RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
        "SB",
        caps=RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
    )
    dev.specials = build_specials_state(RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3, stored=custom_overrides or {})
    return dev


class SendWledResetOverridesTests(unittest.TestCase):
    def test_sends_opc_config_0x0f_unicast_to_last3(self):
        dev = _make_wled(custom_overrides={"wled_fps": 60, "wled_abl_max_ma": 500})
        ctl = _FakeController()

        ok = ctl.sendWledResetOverrides(targetDevice=dev)

        self.assertTrue(ok)
        self.assertEqual(len(ctl.send_config_calls), 1)
        call = ctl.send_config_calls[0]
        self.assertEqual(call["option"], 0x0F)
        self.assertEqual((call["data0"], call["data1"], call["data2"], call["data3"]), (0, 0, 0, 0))
        self.assertEqual(call["recv3"], bytes.fromhex("DDEEFF"))
        self.assertTrue(call["wait_for_ack"])

    def test_resets_wled_specials_to_schema_defaults_on_success(self):
        dev = _make_wled(custom_overrides={
            "wled_fps": 60,
            "wled_abl_max_ma": 500,
            "wled_briS": 200,
            "wled_transition_ms": 1000,
            "wled_seg0_start": 0, "wled_seg0_stop": 18,
        })
        ctl = _FakeController()

        ctl.sendWledResetOverrides(targetDevice=dev)

        # After reset, every wled_* key matches the schema default
        # (FPS=75, ABL=0, briS=128, transition=700, segments 0/0).
        defaults = build_specials_state(RL_Dev_Type.NODE_WLED_REV5, stored={})
        for key, default in defaults.items():
            if key.startswith("wled_"):
                self.assertEqual(dev.specials[key], int(default), f"{key} should reset to {default}")

    def test_preserves_startblock_keys_on_combined_device(self):
        # STARTBLOCK device declares both caps. Reset must touch only
        # wled_* keys and leave startblock_* alone.
        dev = _make_startblock(custom_overrides={
            "startblock_slots": 4,
            "startblock_first_slot": 2,
        })
        ctl = _FakeController()

        ctl.sendWledResetOverrides(targetDevice=dev)

        self.assertEqual(dev.specials["startblock_slots"], 4)
        self.assertEqual(dev.specials["startblock_first_slot"], 2)
        # WLED keys reset to defaults.
        defaults = build_specials_state(RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3, stored={})
        for key, default in defaults.items():
            if key.startswith("wled_"):
                self.assertEqual(dev.specials[key], int(default))

    def test_persists_with_device_specials_scope(self):
        dev = _make_wled()
        ctl = _FakeController()

        ctl.sendWledResetOverrides(targetDevice=dev)

        self.assertEqual(len(ctl.saved), 1)
        args, scopes = ctl.saved[0]
        self.assertEqual(args, {"manual": True})
        self.assertEqual(set(scopes or set()), {state_scope.DEVICE_SPECIALS})

    def test_rejects_target_group(self):
        ctl = _FakeController()
        ok = ctl.sendWledResetOverrides(targetDevice=None, targetGroup=2)
        self.assertFalse(ok)
        self.assertEqual(ctl.send_config_calls, [])

    def test_rejects_non_wled_device(self):
        # Synthesize a device that doesn't declare WLED capability.
        dev = RL_Device(
            "AABBCCDDEEFF",
            RL_Dev_Type.GATEWAY_REV1,  # Gateway has no caps mapping
            "GW",
            caps=RL_Dev_Type.GATEWAY_REV1,
        )
        ctl = _FakeController()

        ok = ctl.sendWledResetOverrides(targetDevice=dev)
        self.assertFalse(ok)
        self.assertEqual(ctl.send_config_calls, [])

    def test_returns_false_on_ack_timeout(self):
        dev = _make_wled(custom_overrides={"wled_fps": 60})
        ctl = _FakeController()
        ctl._sendConfig_returns = False

        ok = ctl.sendWledResetOverrides(targetDevice=dev)
        self.assertFalse(ok)
        # Send was attempted but state must NOT be reset.
        self.assertEqual(len(ctl.send_config_calls), 1)
        self.assertEqual(dev.specials["wled_fps"], 60)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
