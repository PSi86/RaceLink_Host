"""Onboarding-service tests (Stage 1.5 part 2).

Cases A/B/C are exercised via fakes that stand in for the gateway-
service round-trip (set_node_rf_config, set_gateway_rf_config) and
the discovery sweep. The fakes record their inputs so we can assert
on the orchestration order (volatile-switch BEFORE per-node push,
persist-switch AFTER, etc.) — that ordering is the load-bearing
contract of the multi-phase flow.
"""

from __future__ import annotations

import unittest

from racelink.domain import RL_Device
from racelink.services.onboarding_service import OnboardingService
from racelink.state.repository import DeviceRepository


_OLD_CFG = {
    "freq_hz":      867_700_000,
    "bw_khz_x10":   1250,
    "sf":           7,
    "cr_den":       5,
    "sync_word":    0x12,
    "tx_power_dbm": 14,
    "preamble":     8,
}
_NEW_CFG = {
    "freq_hz":      868_500_000,
    "bw_khz_x10":   1250,
    "sf":           7,
    "cr_den":       5,
    "sync_word":    0x34,
    "tx_power_dbm": 14,
    "preamble":     8,
}


class FakeGatewayService:
    """Captures set_gateway_rf_config / set_node_rf_config calls in order."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.next_set_node_result = {"ok": True, "ack_status": 0}
        self.next_set_gateway_result = {"ok": True, "reason_name": "ok"}

    def set_gateway_rf_config(self, rf_config, *, persist=True, timeout_s=None):
        self.calls.append(("gw", persist, dict(rf_config)))
        return dict(self.next_set_gateway_result)

    def set_node_rf_config(self, mac, rf_config, *, timeout_s=None):
        self.calls.append(("node", mac, dict(rf_config)))
        return dict(self.next_set_node_result)


class FakeDiscoveryService:
    """Returns a configurable responder list per discover call."""

    def __init__(self, responders_per_call: list[set[str]] | None = None):
        self.calls: list[dict] = []
        self._queue = list(responders_per_call or [])

    def discover_devices(self, *, group_filter=255, target_device=None, add_to_group=-1):
        self.calls.append({"group_filter": group_filter})
        if self._queue:
            responders = self._queue.pop(0)
        else:
            responders = set()
        return {"found": len(responders), "responders": responders, "assigned_group": None}


class FakeController:
    def __init__(self, devices=None, discovery=None, gateway=None):
        self._device_repository = DeviceRepository(list(devices or []))
        self.discovery_service = discovery if discovery is not None else FakeDiscoveryService([])
        self.gateway_service = gateway if gateway is not None else FakeGatewayService()
        self.group_assignments: list[tuple] = []

    @property
    def device_repository(self):
        return self._device_repository

    def getDeviceFromAddress(self, addr):
        addr = str(addr or "").upper()
        for d in self._device_repository.list():
            if (getattr(d, "addr", "") or "").upper() == addr:
                return d
        return None

    def setNodeGroupId(self, dev, forceSet=False, wait_for_ack=True):
        # Mirror the production return contract: True on ACK_OK,
        # False otherwise. Tests override per-instance.
        self.group_assignments.append((dev.addr, dev.groupId, forceSet, wait_for_ack))
        return True


def _make_device(mac: str, group_id: int = 1) -> RL_Device:
    return RL_Device(addr=mac.upper(), dev_type=11, name=f"WLED {mac[-6:]}", groupId=group_id)


class CaseAReParingTests(unittest.TestCase):

    def test_re_pair_runs_set_group_for_each_target(self):
        devs = [_make_device("AABBCCDDEEFF", group_id=3), _make_device("112233445566", group_id=7)]
        controller = FakeController(devices=devs)
        svc = OnboardingService(controller)

        result = svc.case_a_re_pair(run_discover=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "done")
        # SET_GROUP called once per device with the persisted groupId.
        assigned = sorted(controller.group_assignments)
        self.assertEqual(assigned, sorted([
            ("112233445566", 7, True, True),
            ("AABBCCDDEEFF", 3, True, True),
        ]))
        self.assertEqual(result["summary"]["succeeded"], 2)
        self.assertEqual(result["summary"]["failed"], 0)

    def test_re_pair_subset_via_target_macs(self):
        devs = [_make_device("AABBCCDDEEFF"), _make_device("112233445566")]
        controller = FakeController(devices=devs)
        svc = OnboardingService(controller)

        result = svc.case_a_re_pair(
            target_macs=["AABBCCDDEEFF"], run_discover=False,
        )

        self.assertEqual(len(controller.group_assignments), 1)
        self.assertEqual(controller.group_assignments[0][0], "AABBCCDDEEFF")
        self.assertEqual(result["summary"]["requested"], 1)

    def test_re_pair_set_group_no_ack_flags_stranded(self):
        devs = [_make_device("AABBCCDDEEFF")]
        controller = FakeController(devices=devs)
        # Simulate the node not ACKing the SET_GROUP.
        controller.setNodeGroupId = lambda dev, forceSet=False, wait_for_ack=True: False
        svc = OnboardingService(controller)

        result = svc.case_a_re_pair(run_discover=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["per_device"][0]["status"], "no_ack")
        self.assertEqual(result["stranded"], ["AABBCCDDEEFF"])

    def test_re_pair_with_no_targets_returns_no_targets_stage(self):
        controller = FakeController(devices=[])
        svc = OnboardingService(controller)
        result = svc.case_a_re_pair(run_discover=False)
        self.assertEqual(result["stage"], "no-targets")
        self.assertEqual(result["summary"]["requested"], 0)


class CaseBMigrateTests(unittest.TestCase):

    def test_phase_order_is_volatile_old_then_push_then_persist_new(self):
        # Pre-existing devices live on OLD config; discovery on the
        # OLD channel must surface them so the per-node push can run.
        devs = [_make_device("AABBCCDDEEFF"), _make_device("112233445566")]
        controller = FakeController(
            devices=devs,
            discovery=FakeDiscoveryService([
                {"AABBCCDDEEFF", "112233445566"},  # Phase 2 — on old channel
                {"AABBCCDDEEFF", "112233445566"},  # Phase 5 — back on new channel
            ]),
        )
        svc = OnboardingService(controller)

        # Tiny reboot wait to keep the test fast — patch the constant
        # via monkey-patching the module-level _REBOOT_WAIT_S.
        import racelink.services.onboarding_service as mod
        original_wait = mod._REBOOT_WAIT_S
        mod._REBOOT_WAIT_S = 0.0
        try:
            result = svc.case_b_migrate(old_rf_config=_OLD_CFG, new_rf_config=_NEW_CFG)
        finally:
            mod._REBOOT_WAIT_S = original_wait

        # Phase ordering: volatile-switch to OLD, then 2 node pushes,
        # then persist-switch to NEW.
        gw_calls = controller.gateway_service.calls
        self.assertEqual(gw_calls[0], ("gw", False, _OLD_CFG))   # Phase 1
        node_calls = [c for c in gw_calls if c[0] == "node"]
        self.assertEqual(len(node_calls), 2)                     # Phase 3
        for _, _, cfg in node_calls:
            self.assertEqual(cfg, _NEW_CFG)
        self.assertEqual(gw_calls[-1], ("gw", True, _NEW_CFG))   # Phase 4
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "done")
        self.assertEqual(result["summary"]["pushed_ok"], 2)
        self.assertEqual(result["summary"]["pushed_fail"], 0)
        self.assertEqual(result["stranded"], [])

    def test_phase_3_failure_flags_stranded_and_continues(self):
        devs = [_make_device("AABBCCDDEEFF"), _make_device("112233445566")]
        controller = FakeController(
            devices=devs,
            discovery=FakeDiscoveryService([
                {"AABBCCDDEEFF", "112233445566"},
                {"112233445566"},  # Only one came back on new channel.
            ]),
        )
        # First push succeeds, second fails.
        calls_state = {"n": 0}
        def set_node(mac, rf_config, *, timeout_s=None):
            calls_state["n"] += 1
            controller.gateway_service.calls.append(("node", mac, dict(rf_config)))
            if calls_state["n"] == 1:
                return {"ok": True, "ack_status": 0}
            return {"ok": False, "ack_status": 2, "error": "rejected_range"}
        controller.gateway_service.set_node_rf_config = set_node

        import racelink.services.onboarding_service as mod
        original_wait = mod._REBOOT_WAIT_S
        mod._REBOOT_WAIT_S = 0.0
        try:
            result = svc = OnboardingService(controller).case_b_migrate(
                old_rf_config=_OLD_CFG, new_rf_config=_NEW_CFG,
            )
        finally:
            mod._REBOOT_WAIT_S = original_wait

        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"]["pushed_ok"], 1)
        self.assertEqual(result["summary"]["pushed_fail"], 1)
        statuses = {entry["mac"]: entry["status"] for entry in result["per_device"]}
        # The pushed-ok device came back on new channel → "pushed".
        # The pushed-fail device wasn't checked for stranded (already
        # flagged as push_failed).
        self.assertIn("pushed", statuses.values())
        self.assertIn("push_failed", statuses.values())

    def test_volatile_switch_failure_aborts_early(self):
        devs = [_make_device("AABBCCDDEEFF")]
        controller = FakeController(devices=devs)
        controller.gateway_service.next_set_gateway_result = {
            "ok": False, "reason_name": "rejected_range",
        }
        svc = OnboardingService(controller)

        result = svc.case_b_migrate(old_rf_config=_OLD_CFG, new_rf_config=_NEW_CFG)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "gw-volatile-old-failed")
        # Only the first gateway call was made; no node push, no
        # persist-switch follow-on.
        self.assertEqual(len(controller.gateway_service.calls), 1)


class CaseCAlignGatewayTests(unittest.TestCase):

    def test_persist_switch_succeeds(self):
        controller = FakeController(devices=[_make_device("AABBCCDDEEFF")])
        svc = OnboardingService(controller)

        import racelink.services.onboarding_service as mod
        original_wait = mod._REBOOT_WAIT_S
        mod._REBOOT_WAIT_S = 0.0
        try:
            result = svc.case_c_align_gateway(device_rf_config=_OLD_CFG)
        finally:
            mod._REBOOT_WAIT_S = original_wait

        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "done")
        # Single gateway call with persist=True.
        self.assertEqual(len(controller.gateway_service.calls), 1)
        kind, persist, cfg = controller.gateway_service.calls[0]
        self.assertEqual(kind, "gw")
        self.assertTrue(persist)
        self.assertEqual(cfg, _OLD_CFG)

    def test_persist_switch_failure_aborts(self):
        controller = FakeController()
        controller.gateway_service.next_set_gateway_result = {
            "ok": False, "reason_name": "rejected_range",
        }
        svc = OnboardingService(controller)
        result = svc.case_c_align_gateway(device_rf_config=_OLD_CFG)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "gw-persist-failed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
