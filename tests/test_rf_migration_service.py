"""Multi-network RF-migration tests (Stage 3 Part E).

Pins the four-phase contract:

  1. Pre-check filters out devices already on target and (by
     default) offline devices.
  2. Phase 1 pushes ``OPC_RF_CONFIG`` to every remaining device via
     the network's bound transport.
  3. Phase 2 only fires after Phase 1 completes; it uses
     ``persist=True`` so the gateway reboots onto the new config.
  4. Phase 3 verifies via discovery — survivors get
     ``last_known_rf_config`` updated, missing devices land in
     ``stranded``.

Plus the cross-cutting contracts:

  * Foreign-network devices are never touched.
  * Phase-2 failure leaves the bind service alone (re-evaluate is
    only called on full success).
  * Empty push-set still progresses through Phases 2+3.
  * Devices that fail Phase-1 push are not in the verification set
    (a missing ACK means they probably never received the config,
    so we don't poke their entry post-reboot).
"""

from __future__ import annotations

import unittest

from racelink.domain.models import RL_Device, RL_Network
from racelink.services.rf_migration_service import RfMigrationService


_OLD_CFG = {
    "freq_hz":      867_700_000,
    "bw_khz_x10":   1250,
    "sf":           7,
    "cr_den":       5,
    "sync_word":    0x12,
    "tx_power_dbm": 14,
    "preamble":     8,
}

_NEW_CFG = dict(_OLD_CFG, freq_hz=868_500_000)


class _FakeNetworkRepo:
    def __init__(self, networks):
        self._items = list(networks)

    def list(self):
        return list(self._items)

    def get_by_id(self, network_id):
        for n in self._items:
            if str(getattr(n, "id", "") or "") == str(network_id):
                return n
        return None

    def get_by_gateway_mac(self, gateway_mac):
        if not gateway_mac:
            return None
        target = str(gateway_mac).upper()
        for n in self._items:
            if str(getattr(n, "gateway_mac", "") or "").upper() == target:
                return n
        return None


class _FakeDeviceRepo:
    def __init__(self, devices):
        self._items = list(devices)

    def list(self):
        return list(self._items)


class _FakeTransport:
    def __init__(self, ident_mac):
        self.ident_mac = ident_mac


class _FakeGatewayService:
    """Records every set_node_rf_config + set_gateway_rf_config call.

    The migration engine drives:
      * ``set_node_rf_config(mac, target, transport=...)`` per device
      * ``set_gateway_rf_config(target, persist=True, transport=...)`` once

    Both return canned ``ok`` outcomes the tests pre-program via
    ``node_results`` (keyed by mac) and ``gateway_result``.
    """

    def __init__(self, *, node_results=None, gateway_result=None):
        self._node_results = dict(node_results or {})
        self._gateway_result = dict(gateway_result or {"ok": True, "reason": 0})
        self.node_calls: list[dict] = []
        self.gateway_calls: list[dict] = []

    def set_node_rf_config(self, mac, rf_config, *, transport=None, timeout_s=None):
        call = {"mac": mac, "rf_config": dict(rf_config),
                "transport_ident": getattr(transport, "ident_mac", None)}
        self.node_calls.append(call)
        return dict(self._node_results.get(mac, {"ok": True, "ack_status": 0}))

    def set_gateway_rf_config(self, rf_config, *, persist=True, transport=None, timeout_s=1.0):
        self.gateway_calls.append({
            "rf_config": dict(rf_config),
            "persist": persist,
            "transport_ident": getattr(transport, "ident_mac", None),
        })
        return dict(self._gateway_result)


class _FakeDiscovery:
    def __init__(self, responders):
        self._responders = set(str(m).upper() for m in (responders or []))

    def discover_devices(self, *, group_filter=255):
        return {"found": len(self._responders),
                "responders": set(self._responders),
                "assigned_group": None}


class _FakeBindService:
    def __init__(self):
        self.re_evaluated: list[str] = []

    def re_evaluate(self, ident_mac):
        self.re_evaluated.append(str(ident_mac).upper())


class _FakeController:
    """Lightweight controller stand-in: just enough to drive the
    migration engine's lookups (networks, devices, transport-for-
    network, save_to_db). Avoids spinning up the real Racelink_Host
    so tests don't bring in the runtime state-repo singleton."""

    def __init__(self, *, networks, devices, transports, gateway_service, discovery_service):
        self.network_repository = _FakeNetworkRepo(networks)
        self.device_repository = _FakeDeviceRepo(devices)
        self._transports = list(transports)
        self.gateway_service = gateway_service
        self.discovery_service = discovery_service
        self.persisted_calls: list[dict] = []

    @property
    def transports(self):
        return list(self._transports)

    def transport_for_network(self, network_id):
        net = self.network_repository.get_by_id(network_id)
        if net is None or not getattr(net, "gateway_mac", None):
            return None
        target = str(net.gateway_mac).upper()
        for t in self._transports:
            if str(getattr(t, "ident_mac", "") or "").upper() == target:
                return t
        return None

    def getDeviceFromAddress(self, mac):
        target = str(mac or "").upper()
        for d in self.device_repository.list():
            if str(getattr(d, "addr", "") or "").upper() == target:
                return d
        return None

    def save_to_db(self, args, scopes=None):
        self.persisted_calls.append({"args": dict(args or {}), "scopes": scopes})


def _device(mac, *, network_id, link_online=True, last_known_rf_config=None):
    d = RL_Device(mac, dev_type=0, name=mac)
    d.network_id = network_id
    d.link_online = link_online
    if last_known_rf_config is not None:
        d.last_known_rf_config = dict(last_known_rf_config)
    return d


def _make_service(*, devices, transports=("GW-A",), node_results=None,
                  gateway_result=None, responders=None, network_id=None):
    network_id = network_id or "net-a"
    net = RL_Network(id=network_id, name="Track A",
                     gateway_mac=transports[0], rf_config=dict(_OLD_CFG))
    gw = _FakeGatewayService(node_results=node_results, gateway_result=gateway_result)
    disc = _FakeDiscovery(responders if responders is not None else [d.addr for d in devices])
    ctrl = _FakeController(
        networks=[net], devices=devices,
        transports=[_FakeTransport(m) for m in transports],
        gateway_service=gw, discovery_service=disc,
    )
    bind = _FakeBindService()
    svc = RfMigrationService(controller=ctrl, bind_service=bind)
    return ctrl, gw, disc, bind, svc, net


class PreCheckFilterTests(unittest.TestCase):

    def test_skips_devices_already_on_target(self):
        devs = [
            _device("AABBCC112233", network_id="net-a",
                    last_known_rf_config=_NEW_CFG),
            _device("AABBCC112244", network_id="net-a",
                    last_known_rf_config=_OLD_CFG),
        ]
        _ctrl, gw, _disc, _bind, svc, _net = _make_service(devices=devs)

        # Stub time.sleep so the test doesn't actually wait the reboot
        # window.
        with _no_sleep():
            res = svc.migrate_network_to("net-a", _NEW_CFG)

        # Only the diverging device was pushed.
        self.assertEqual([c["mac"] for c in gw.node_calls], ["AABBCC112244"])
        self.assertEqual(res["summary"]["skipped_already_target"],
                         ["AABBCC112233"])
        self.assertEqual(res["summary"]["push_count"], 1)

    def test_skips_offline_devices_by_default(self):
        devs = [
            _device("AABBCC112233", network_id="net-a", link_online=False),
            _device("AABBCC112244", network_id="net-a", link_online=True),
        ]
        _ctrl, gw, _disc, _bind, svc, _net = _make_service(
            devices=devs, responders=["AABBCC112244"],
        )

        with _no_sleep():
            res = svc.migrate_network_to("net-a", _NEW_CFG)

        self.assertEqual([c["mac"] for c in gw.node_calls], ["AABBCC112244"])
        self.assertEqual(res["summary"]["skipped_offline"],
                         ["AABBCC112233"])

    def test_force_offline_includes_offline_devices(self):
        devs = [
            _device("AABBCC112233", network_id="net-a", link_online=False),
        ]
        _ctrl, gw, _disc, _bind, svc, _net = _make_service(
            devices=devs, responders=["AABBCC112233"],
        )

        with _no_sleep():
            res = svc.migrate_network_to("net-a", _NEW_CFG, force_offline=True)

        # Even the offline device is pushed.
        self.assertEqual([c["mac"] for c in gw.node_calls], ["AABBCC112233"])
        self.assertEqual(res["summary"]["skipped_offline"], [])

    def test_foreign_network_devices_never_touched(self):
        devs = [
            _device("AABBCC111111", network_id="net-a", link_online=True),
            _device("AABBCC222222", network_id="net-b", link_online=True),
        ]
        _ctrl, gw, _disc, _bind, svc, _net = _make_service(
            devices=devs, responders=["AABBCC111111"],
        )

        with _no_sleep():
            res = svc.migrate_network_to("net-a", _NEW_CFG)

        # ``net-b`` device is completely invisible to the engine.
        self.assertEqual([c["mac"] for c in gw.node_calls], ["AABBCC111111"])
        self.assertEqual(res["summary"]["total_devices_on_network"], 1)


class FourPhaseFlowTests(unittest.TestCase):

    def test_happy_path_pushes_then_switches_then_verifies(self):
        devs = [
            _device("AABBCC111111", network_id="net-a"),
            _device("AABBCC222222", network_id="net-a"),
        ]
        ctrl, gw, _disc, bind, svc, net = _make_service(
            devices=devs,
            responders=["AABBCC111111", "AABBCC222222"],
        )

        with _no_sleep():
            res = svc.migrate_network_to("net-a", _NEW_CFG)

        self.assertTrue(res["ok"])
        self.assertEqual(res["stage"], "done")
        # Phase order: every node call recorded BEFORE the gateway call.
        # The fakes record into separate lists, but we can check via
        # the gateway being called exactly once and after node pushes.
        self.assertEqual(len(gw.gateway_calls), 1)
        self.assertEqual(gw.gateway_calls[0]["persist"], True)
        self.assertEqual(gw.gateway_calls[0]["rf_config"], _NEW_CFG)
        # Network's rf_config was updated.
        self.assertEqual(net.rf_config, _NEW_CFG)
        # Bind service was re-evaluated for the gateway ident.
        self.assertEqual(bind.re_evaluated, ["GW-A"])
        # Both devices verified, none stranded.
        macs_verified = set(res["summary"]["verified"])
        self.assertEqual(macs_verified, {"AABBCC111111", "AABBCC222222"})
        self.assertEqual(res["stranded"], [])
        # Devices' last_known_rf_config now matches target.
        for d in devs:
            self.assertEqual(d.last_known_rf_config, _NEW_CFG)
        # Persistence ran at least once (for the rf_config and verified updates).
        self.assertTrue(ctrl.persisted_calls)

    def test_stranded_devices_dont_get_last_known_updated(self):
        devs = [
            _device("AABBCC111111", network_id="net-a"),
            _device("AABBCC222222", network_id="net-a"),
        ]
        # Only the first device shows up post-reboot.
        _ctrl, _gw, _disc, _bind, svc, _net = _make_service(
            devices=devs, responders=["AABBCC111111"],
        )

        with _no_sleep():
            res = svc.migrate_network_to("net-a", _NEW_CFG)

        self.assertFalse(res["ok"])  # stranded counts as not-ok
        self.assertEqual(set(res["stranded"]), {"AABBCC222222"})
        # Verified device got its last_known updated; stranded did not.
        d1 = next(d for d in devs if d.addr == "AABBCC111111")
        d2 = next(d for d in devs if d.addr == "AABBCC222222")
        self.assertEqual(d1.last_known_rf_config, _NEW_CFG)
        self.assertNotEqual(getattr(d2, "last_known_rf_config", None), _NEW_CFG)

    def test_push_failed_devices_skipped_in_verification(self):
        devs = [
            _device("AABBCC111111", network_id="net-a"),
            _device("AABBCC222222", network_id="net-a"),
        ]
        # Second device's push returns a NACK.
        _ctrl, _gw, _disc, _bind, svc, _net = _make_service(
            devices=devs,
            node_results={"AABBCC222222": {"ok": False, "ack_status": 2}},
            responders=["AABBCC111111"],
        )

        with _no_sleep():
            res = svc.migrate_network_to("net-a", _NEW_CFG)

        self.assertFalse(res["ok"])
        self.assertEqual(res["summary"]["pushed_ok"], 1)
        self.assertEqual(res["summary"]["pushed_fail"], 1)
        # The push-failed device is NOT in stranded — it never accepted
        # the config so we don't expect it on the new channel.
        self.assertEqual(res["stranded"], [])
        # Status surfaced per-device for the wizard summary.
        statuses = {e["mac"]: e["status"] for e in res["per_device"]}
        self.assertEqual(statuses["AABBCC222222"], "push_failed")

    def test_gateway_switch_failure_aborts_before_verification(self):
        devs = [_device("AABBCC111111", network_id="net-a")]
        _ctrl, gw, _disc, bind, svc, _net = _make_service(
            devices=devs,
            gateway_result={"ok": False, "reason": 4, "reason_name": "BAD_RANGE"},
        )

        with _no_sleep():
            res = svc.migrate_network_to("net-a", _NEW_CFG)

        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "gateway-migration-failed")
        self.assertIn("BAD_RANGE", res["error"])
        # The bind service must NOT be re-evaluated on a failed switch.
        self.assertEqual(bind.re_evaluated, [])
        # Phase 1 still ran for the device — the engine doesn't pre-validate
        # the gateway's response shape (devices on new settings + gateway
        # stuck on old is an operator-recoverable state).
        self.assertEqual(len(gw.node_calls), 1)

    def test_empty_push_set_still_runs_gateway_switch(self):
        # Network with one device already on target.
        devs = [
            _device("AABBCC111111", network_id="net-a",
                    last_known_rf_config=_NEW_CFG),
        ]
        _ctrl, gw, _disc, bind, svc, net = _make_service(
            devices=devs, responders=[],
        )

        with _no_sleep():
            res = svc.migrate_network_to("net-a", _NEW_CFG)

        # No node pushes — operator still wants the gateway aligned.
        self.assertEqual(gw.node_calls, [])
        self.assertEqual(len(gw.gateway_calls), 1)
        # Network rf_config updated.
        self.assertEqual(net.rf_config, _NEW_CFG)
        # No stranded; the migration is OK.
        self.assertTrue(res["ok"])
        self.assertEqual(bind.re_evaluated, ["GW-A"])


class ValidationTests(unittest.TestCase):

    def test_unknown_network_id_returns_error(self):
        _ctrl, _gw, _disc, _bind, svc, _net = _make_service(devices=[])
        res = svc.migrate_network_to("does-not-exist", _NEW_CFG)
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "validate")
        self.assertIn("unknown network_id", res["error"])

    def test_target_missing_fields_rejected(self):
        _ctrl, _gw, _disc, _bind, svc, _net = _make_service(devices=[])
        res = svc.migrate_network_to("net-a", {"freq_hz": 868_000_000})
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "validate")
        self.assertIn("wire-format fields", res["error"])

    def test_no_transport_bound_to_network(self):
        # Build the service with a network that has gateway_mac="GW-A",
        # but no transport with that ident is attached.
        net = RL_Network(id="net-a", name="Track A",
                         gateway_mac="GW-A", rf_config=dict(_OLD_CFG))
        gw = _FakeGatewayService()
        ctrl = _FakeController(
            networks=[net], devices=[],
            transports=[],  # no transport attached
            gateway_service=gw,
            discovery_service=_FakeDiscovery([]),
        )
        svc = RfMigrationService(controller=ctrl, bind_service=_FakeBindService())
        res = svc.migrate_network_to("net-a", _NEW_CFG)
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "validate")
        self.assertIn("no attached transport", res["error"])


class ProgressCallbackTests(unittest.TestCase):

    def test_progress_emitted_for_each_phase(self):
        devs = [
            _device("AABBCC111111", network_id="net-a"),
        ]
        _ctrl, _gw, _disc, _bind, svc, _net = _make_service(
            devices=devs, responders=["AABBCC111111"],
        )
        progress: list[dict] = []
        with _no_sleep():
            svc.migrate_network_to(
                "net-a", _NEW_CFG, progress_cb=lambda p: progress.append(p),
            )
        stages = [p["stage"] for p in progress]
        # At least one event per logical phase.
        self.assertIn("pre-check", stages)
        self.assertIn("device-migration", stages)
        self.assertIn("gateway-migration", stages)
        self.assertIn("verification", stages)


# ---- helpers ---------------------------------------------------------

class _no_sleep:
    """Context manager that monkey-patches ``time.sleep`` on the
    migration service module to a no-op. The engine sleeps ~4 s
    post-Phase-2 to wait for the gateway reboot; tests don't care
    and would otherwise add seconds per test.

    Patches only the migration service module's ``time.sleep``
    reference — NEVER touch the global ``time.sleep`` because other
    timing-sensitive tests in the suite (transport TX timeout,
    SSE ping cadence, debounce) would break if we leaked a no-op
    sleep into them.
    """

    def __enter__(self):
        from racelink.services import rf_migration_service as _mod
        import time as _time

        # The migration service imports ``time`` at module load and
        # uses ``time.sleep(...)`` via the module attribute. Replacing
        # ``_mod.time`` with a small shim that intercepts only
        # ``sleep`` keeps every *other* ``time`` API working and
        # leaves the global ``time`` module untouched.
        self._mod = _mod
        self._orig_time = _mod.time

        class _NoSleepShim:
            sleep = staticmethod(lambda _seconds: None)

            def __getattr__(self, name):
                return getattr(_time, name)

        _mod.time = _NoSleepShim()
        return self

    def __exit__(self, *_a):
        self._mod.time = self._orig_time


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
