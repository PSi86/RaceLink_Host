"""Gateway-bind state machine tests (Stage 3 Part D).

Pins the three primary cases from the plan:

  * Case A — known gateway + matching rf_config → BOUND, SSE
    ``gateway_bound``.
  * Case B — known gateway + diverging rf_config → CONFLICT, SSE
    ``gateway_conflict``. Resolve actions ``accept_gateway`` and
    ``accept_host`` are exercised; ``accept_host`` parks the
    intent for the Stage-3 Part-E migration engine.
  * Case C — unknown gateway → UNBOUND, SSE ``gateway_unbound``.
    Resolve actions ``create_network`` and ``rebind`` are
    exercised.

Plus the cross-cutting contracts:

  * Token gating refuses stale wizard answers.
  * First-contact adoption: a network with no ``rf_config`` silently
    inherits the gateway's reported config (no operator wizard).
  * Forget drops the bind record so a future re-attach starts
    fresh.
"""

from __future__ import annotations

import unittest

from racelink.domain.models import RL_Network
from racelink.services.gateway_bind_service import (
    BindState,
    GatewayBindService,
)


_HOST_CFG = {
    "freq_hz":      867_700_000,
    "bw_khz_x10":   1250,
    "sf":           7,
    "cr_den":       5,
    "sync_word":    0x12,
    "tx_power_dbm": 14,
    "preamble":     8,
}

_GATEWAY_CFG_MATCH = dict(_HOST_CFG)
_GATEWAY_CFG_DIVERGE = dict(_HOST_CFG, freq_hz=868_500_000, sync_word=0x34)


class _FakeNetworkRepository:
    def __init__(self):
        self._items: list[RL_Network] = []

    def list(self):
        return list(self._items)

    def append(self, net):
        self._items.append(net)
        return net

    def get_by_id(self, network_id):
        target = str(network_id or "")
        for n in self._items:
            if str(getattr(n, "id", "") or "") == target:
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


class _FakeTransport:
    def __init__(self, ident_mac, *, network_id=None, port="COM3"):
        self.ident_mac = ident_mac
        self.network_id = network_id
        self.port = port


class _FakeTaskManager:
    """Mirrors the subset of ``racelink.web.tasks.TaskManager`` that
    ``_action_accept_host`` touches: ``is_running``, ``start``,
    ``update``. Tracks every call so tests can assert ordering."""

    def __init__(self, *, start_returns_none=False):
        self.start_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self._running = False
        self._start_returns_none = start_returns_none

    def is_running(self) -> bool:
        return self._running

    def start(self, name, target_fn, meta=None):
        self.start_calls.append({
            "name": name,
            "target_fn": target_fn,
            "meta": dict(meta or {}),
        })
        if self._start_returns_none:
            return None
        self._running = True
        return {
            "id": len(self.start_calls),
            "name": name,
            "state": "running",
            "meta": dict(meta or {}),
        }

    def update(self, **updates):
        self.update_calls.append(dict(updates))

    def force_busy(self):
        self._running = True


class _FakeRfMigrationService:
    """Records the migrate_network_to calls; never actually runs."""

    def __init__(self):
        self.calls: list[dict] = []

    def migrate_network_to(self, *, network_id, target_rf_config, progress_cb=None,
                           force_offline=False):
        self.calls.append({
            "network_id": network_id,
            "target_rf_config": dict(target_rf_config),
            "progress_cb": progress_cb,
            "force_offline": force_offline,
        })
        return {"ok": True, "summary": {}}


class _FakeDevice:
    def __init__(self, addr, *, network_id=None, configByte=0, name="",
                 last_seen_ts=0.0):
        self.addr = addr
        self.network_id = network_id
        self.configByte = configByte
        self.name = name or f"dev-{addr}"
        self.last_seen_ts = last_seen_ts


class _FakeDeviceRepository:
    def __init__(self, devices=()):
        self._items = list(devices)

    def list(self):
        return list(self._items)

    def append(self, dev):
        self._items.append(dev)
        return dev


class _FakeController:
    def __init__(self, *, task_manager=None, rf_migration_service=None,
                 devices=()):
        self.network_repository = _FakeNetworkRepository()
        self.device_repository = _FakeDeviceRepository(devices)
        self._transports: list[_FakeTransport] = []
        self._task_manager = task_manager
        self.rf_migration_service = rf_migration_service

    @property
    def transports(self):
        return self._transports


class _FakeGatewayService:
    """Returns a canned ``rf_config`` per ident_mac via
    ``query_gateway_rf_config(transport=...)``."""

    def __init__(self, configs, *, set_result=None):
        # ``configs`` maps ident_mac -> rf_config dict (or None for
        # "no reply"). ``None`` simulates a timeout / pre-handshake.
        self._configs = dict(configs or {})
        # ``set_result`` overrides the canned reply of
        # ``set_gateway_rf_config`` so tests can drive the reject path.
        self._set_result = set_result
        self.set_calls: list[dict] = []

    def query_gateway_rf_config(self, *, transport=None, timeout_s=0.5):
        ident = getattr(transport, "ident_mac", None) if transport else None
        cfg = self._configs.get(ident)
        if cfg is None:
            return {"ok": False, "error": "no reply within timeout"}
        return {"ok": True, "rf_config": dict(cfg)}

    def set_gateway_rf_config(self, rf_config, *, persist=True, transport=None):
        ident = getattr(transport, "ident_mac", None) if transport else None
        self.set_calls.append({
            "ident_mac": ident,
            "rf_config": dict(rf_config),
            "persist": persist,
        })
        if self._set_result is not None:
            return dict(self._set_result)
        # Mirror the real gateway: the write is committed and echoed
        # back before the reboot drops the link.
        self._configs[ident] = dict(rf_config)
        return {"ok": True, "rf_config": dict(rf_config)}


def _make_service(*, ctrl=None, gw=None, broadcasts=None, persists=None):
    ctrl = ctrl or _FakeController()
    gw = gw or _FakeGatewayService(configs={})
    broadcasts = broadcasts if broadcasts is not None else []
    persists = persists if persists is not None else []

    def _broadcast(event_name, payload):
        broadcasts.append((event_name, payload))

    def _persist():
        persists.append(True)

    svc = GatewayBindService(
        controller=ctrl,
        gateway_service=gw,
        broadcast=_broadcast,
        persist=_persist,
    )
    return ctrl, gw, svc, broadcasts, persists


class CaseAKnownGatewayMatchingTests(unittest.TestCase):

    def test_bound_when_rf_config_matches(self):
        ctrl, _gw, svc, broadcasts, persists = _make_service(
            gw=_FakeGatewayService(configs={"GW-A": _GATEWAY_CFG_MATCH}),
        )
        net = RL_Network(name="Track A", gateway_mac="GW-A",
                         rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(net)
        t = _FakeTransport("GW-A", network_id=net.id)
        ctrl._transports.append(t)

        rec = svc.evaluate(t)

        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertEqual(rec.state, BindState.BOUND)
        self.assertEqual(rec.network_id, net.id)
        self.assertEqual(rec.conflict_fields, [])
        # SSE event broadcast exactly once.
        self.assertEqual(len(broadcasts), 1)
        ev_name, payload = broadcasts[0]
        self.assertEqual(ev_name, GatewayBindService.EVENT_BOUND)
        self.assertEqual(payload["ident_mac"], "GW-A")
        self.assertEqual(payload["state"], "bound")
        # No persist needed when nothing changed.
        self.assertEqual(persists, [])


class CaseBKnownGatewayConflictTests(unittest.TestCase):

    def test_conflict_when_rf_config_diverges(self):
        ctrl, _gw, svc, broadcasts, _persists = _make_service(
            gw=_FakeGatewayService(configs={"GW-A": _GATEWAY_CFG_DIVERGE}),
        )
        net = RL_Network(name="Track A", gateway_mac="GW-A",
                         rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(net)
        t = _FakeTransport("GW-A", network_id=net.id)
        ctrl._transports.append(t)

        rec = svc.evaluate(t)

        assert rec is not None
        self.assertEqual(rec.state, BindState.CONFLICT)
        # Diff includes the two fields we changed.
        self.assertEqual(set(rec.conflict_fields), {"freq_hz", "sync_word"})
        ev_name, payload = broadcasts[-1]
        self.assertEqual(ev_name, GatewayBindService.EVENT_CONFLICT)
        self.assertEqual(set(payload["conflict_fields"]),
                         {"freq_hz", "sync_word"})

    def test_resolve_accept_gateway_adopts_actual_config(self):
        ctrl, _gw, svc, broadcasts, persists = _make_service(
            gw=_FakeGatewayService(configs={"GW-A": _GATEWAY_CFG_DIVERGE}),
        )
        net = RL_Network(name="Track A", gateway_mac="GW-A",
                         rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(net)
        t = _FakeTransport("GW-A", network_id=net.id)
        ctrl._transports.append(t)
        rec_initial = svc.evaluate(t)
        assert rec_initial is not None
        token = rec_initial.token

        out = svc.resolve("GW-A", "accept_gateway", {"token": token})

        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], "bound")
        # Network adopted the gateway's actual config; conflict cleared.
        self.assertEqual(net.rf_config, _GATEWAY_CFG_DIVERGE)
        rec = svc.get("GW-A")
        assert rec is not None
        self.assertEqual(rec.state, BindState.BOUND)
        self.assertEqual(rec.conflict_fields, [])
        # Persistence fired (network mutated).
        self.assertTrue(persists)
        # SSE: the conflict event then a fresh bound event.
        self.assertEqual(broadcasts[-1][0], GatewayBindService.EVENT_BOUND)

    def test_resolve_accept_host_starts_migration_task(self):
        tasks = _FakeTaskManager()
        migration = _FakeRfMigrationService()
        ctrl = _FakeController(task_manager=tasks, rf_migration_service=migration)
        _ctrl, _gw, svc, _broadcasts, _persists = _make_service(
            ctrl=ctrl,
            gw=_FakeGatewayService(configs={"GW-A": _GATEWAY_CFG_DIVERGE}),
        )
        net = RL_Network(name="Track A", gateway_mac="GW-A",
                         rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(net)
        t = _FakeTransport("GW-A", network_id=net.id)
        ctrl._transports.append(t)
        rec_initial = svc.evaluate(t)
        assert rec_initial is not None

        out = svc.resolve("GW-A", "accept_host", {"token": rec_initial.token})

        self.assertTrue(out["ok"])
        # State stays CONFLICT; migration_pending flips on. The
        # migration engine flips state to BOUND on completion.
        self.assertEqual(out["state"], "conflict")
        self.assertTrue(out["migration_pending"])
        # Wizard receives a task_id so it can subscribe to the SSE
        # ``task`` channel and render live progress.
        self.assertIn("task_id", out)
        self.assertIsNotNone(out["task_id"])
        rec = svc.get("GW-A")
        assert rec is not None
        self.assertTrue(rec.migration_pending)
        # Network's rf_config is unchanged (still the host's).
        self.assertEqual(net.rf_config, _HOST_CFG)
        # TaskManager.start was called exactly once with the right name.
        self.assertEqual(len(tasks.start_calls), 1)
        self.assertEqual(tasks.start_calls[0]["name"], "rf_migration")
        self.assertEqual(tasks.start_calls[0]["meta"]["network_id"], net.id)
        self.assertEqual(tasks.start_calls[0]["meta"]["ident_mac"], "GW-A")
        # The runner is wired up but the FakeTaskManager doesn't spawn
        # a thread, so migration.migrate_network_to is not invoked
        # synchronously here. Invoke the runner to verify wiring.
        runner = tasks.start_calls[0]["target_fn"]
        runner()
        self.assertEqual(len(migration.calls), 1)
        self.assertEqual(migration.calls[0]["network_id"], net.id)
        self.assertEqual(migration.calls[0]["target_rf_config"], _HOST_CFG)
        self.assertTrue(callable(migration.calls[0]["progress_cb"]))

    def test_resolve_accept_host_refuses_when_task_manager_busy(self):
        tasks = _FakeTaskManager()
        tasks.force_busy()
        migration = _FakeRfMigrationService()
        ctrl = _FakeController(task_manager=tasks, rf_migration_service=migration)
        _ctrl, _gw, svc, _broadcasts, _persists = _make_service(
            ctrl=ctrl,
            gw=_FakeGatewayService(configs={"GW-A": _GATEWAY_CFG_DIVERGE}),
        )
        net = RL_Network(name="Track A", gateway_mac="GW-A",
                         rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(net)
        t = _FakeTransport("GW-A", network_id=net.id)
        ctrl._transports.append(t)
        rec_initial = svc.evaluate(t)
        assert rec_initial is not None

        out = svc.resolve("GW-A", "accept_host", {"token": rec_initial.token})

        self.assertFalse(out["ok"])
        self.assertIn("busy", out["error"])
        self.assertEqual(out["state"], "conflict")
        self.assertEqual(tasks.start_calls, [])
        self.assertEqual(migration.calls, [])
        # migration_pending was NOT set since the kickoff was refused.
        rec = svc.get("GW-A")
        assert rec is not None
        self.assertFalse(rec.migration_pending)

    def test_resolve_accept_host_errors_when_services_missing(self):
        # Default _FakeController has no task_manager / rf_migration_service.
        ctrl, _gw, svc, _broadcasts, _persists = _make_service(
            gw=_FakeGatewayService(configs={"GW-A": _GATEWAY_CFG_DIVERGE}),
        )
        net = RL_Network(name="Track A", gateway_mac="GW-A",
                         rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(net)
        t = _FakeTransport("GW-A", network_id=net.id)
        ctrl._transports.append(t)
        rec_initial = svc.evaluate(t)
        assert rec_initial is not None

        out = svc.resolve("GW-A", "accept_host", {"token": rec_initial.token})

        self.assertFalse(out["ok"])
        self.assertIn("not ready", out["error"])
        rec = svc.get("GW-A")
        assert rec is not None
        self.assertFalse(rec.migration_pending)


class CaseCUnknownGatewayTests(unittest.TestCase):

    def test_unbound_when_no_network_matches(self):
        ctrl, _gw, svc, broadcasts, _persists = _make_service(
            gw=_FakeGatewayService(configs={"GW-X": _GATEWAY_CFG_MATCH}),
        )
        # Transport carries no network_id (auto-bind refused).
        t = _FakeTransport("GW-X", network_id=None)
        ctrl._transports.append(t)

        rec = svc.evaluate(t)

        assert rec is not None
        self.assertEqual(rec.state, BindState.UNBOUND)
        self.assertIsNone(rec.network_id)
        # SSE event for the wizard.
        ev_name, payload = broadcasts[-1]
        self.assertEqual(ev_name, GatewayBindService.EVENT_UNBOUND)
        self.assertEqual(payload["ident_mac"], "GW-X")
        # rf_config_actual is still recorded so the wizard can show
        # what the gateway is on.
        self.assertEqual(rec.rf_config_actual, _GATEWAY_CFG_MATCH)

    def test_resolve_create_network_binds_and_persists(self):
        ctrl, _gw, svc, broadcasts, persists = _make_service(
            gw=_FakeGatewayService(configs={"GW-X": _GATEWAY_CFG_MATCH}),
        )
        t = _FakeTransport("GW-X")
        ctrl._transports.append(t)
        rec_initial = svc.evaluate(t)
        assert rec_initial is not None

        out = svc.resolve("GW-X", "create_network", {
            "token": rec_initial.token,
            "name": "Heat 1",
            "region": "EU868",
        })

        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], "bound")
        # Network was created with the gateway's reported config.
        nets = ctrl.network_repository.list()
        self.assertEqual(len(nets), 1)
        self.assertEqual(nets[0].name, "Heat 1")
        self.assertEqual(nets[0].gateway_mac, "GW-X")
        self.assertEqual(nets[0].rf_config, _GATEWAY_CFG_MATCH)
        # Transport's back-reference was stamped.
        self.assertEqual(t.network_id, nets[0].id)
        # Persist + SSE bound event.
        self.assertTrue(persists)
        self.assertEqual(broadcasts[-1][0], GatewayBindService.EVENT_BOUND)

    def test_resolve_create_network_errors_when_gateway_did_not_reply(self):
        # Gateway maps to None — _FakeGatewayService returns ok=False.
        # The bind service therefore records rf_config_actual=None.
        ctrl, _gw, svc, broadcasts, persists = _make_service(
            gw=_FakeGatewayService(configs={"GW-X": None}),
        )
        t = _FakeTransport("GW-X")
        ctrl._transports.append(t)
        rec_initial = svc.evaluate(t)
        assert rec_initial is not None
        self.assertIsNone(rec_initial.rf_config_actual)
        broadcasts_before = list(broadcasts)
        persists_before = list(persists)

        out = svc.resolve("GW-X", "create_network", {
            "token": rec_initial.token,
            "name": "Heat 1",
            "region": "EU868",
        })

        self.assertFalse(out["ok"])
        self.assertIn("GET_RF_CONFIG", out["error"])
        # State and network repo are untouched — the operator can plug
        # the hardware, wait for a proper readback, and retry.
        self.assertEqual(ctrl.network_repository.list(), [])
        rec = svc.get("GW-X")
        assert rec is not None
        self.assertEqual(rec.state, BindState.UNBOUND)
        # No extra broadcast / persist beyond the initial evaluate.
        self.assertEqual(broadcasts, broadcasts_before)
        self.assertEqual(persists, persists_before)

    def test_resolve_rebind_refuses_ethernet_network(self):
        # Compatibility guard: an RF gateway must never be bound to an
        # Ethernet network (no gateway_mac, host-NIC transport). The
        # resolve call is rejected and the Ethernet network is left
        # untouched.
        ctrl, _gw, svc, broadcasts, persists = _make_service(
            gw=_FakeGatewayService(configs={"GW-X": _GATEWAY_CFG_MATCH}),
        )
        eth = RL_Network(name="Pit LAN", kind="ethernet")
        ctrl.network_repository.append(eth)
        t = _FakeTransport("GW-X")
        ctrl._transports.append(t)
        rec_initial = svc.evaluate(t)
        assert rec_initial is not None
        broadcasts_before = list(broadcasts)
        persists_before = list(persists)

        out = svc.resolve("GW-X", "rebind", {
            "token": rec_initial.token,
            "network_id": eth.id,
        })

        self.assertFalse(out["ok"])
        self.assertIn("ethernet", out["error"].lower())
        # The gateway stays UNBOUND; the Ethernet network never gains a
        # gateway_mac and the transport is not stamped.
        self.assertEqual(out["state"], "unbound")
        self.assertIsNone(eth.gateway_mac)
        self.assertIsNone(t.network_id)
        rec = svc.get("GW-X")
        assert rec is not None
        self.assertEqual(rec.state, BindState.UNBOUND)
        # No side effects beyond the initial evaluate.
        self.assertEqual(broadcasts, broadcasts_before)
        self.assertEqual(persists, persists_before)

    def test_resolve_rebind_to_existing_network_first_contact_adopt(self):
        ctrl, _gw, svc, _broadcasts, _persists = _make_service(
            gw=_FakeGatewayService(configs={"GW-X": _GATEWAY_CFG_MATCH}),
        )
        # Pre-existing network with no rf_config and a stale gateway_mac.
        existing = RL_Network(name="Track Z", gateway_mac="OLD-MAC",
                              rf_config=None)
        ctrl.network_repository.append(existing)
        t = _FakeTransport("GW-X")
        ctrl._transports.append(t)
        rec_initial = svc.evaluate(t)
        assert rec_initial is not None

        out = svc.resolve("GW-X", "rebind", {
            "token": rec_initial.token,
            "network_id": existing.id,
        })

        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], "bound")
        # The rebind moved the gateway_mac AND adopted the gateway's config.
        self.assertEqual(existing.gateway_mac, "GW-X")
        self.assertEqual(existing.rf_config, _GATEWAY_CFG_MATCH)
        self.assertEqual(t.network_id, existing.id)

    def test_resolve_rebind_to_existing_network_with_conflict(self):
        ctrl, _gw, svc, _broadcasts, _persists = _make_service(
            gw=_FakeGatewayService(configs={"GW-X": _GATEWAY_CFG_DIVERGE}),
        )
        # Existing network already has a different rf_config; the
        # rebind brings the gateway in but flags the divergence.
        existing = RL_Network(name="Track Z", gateway_mac=None,
                              rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(existing)
        t = _FakeTransport("GW-X")
        ctrl._transports.append(t)
        rec_initial = svc.evaluate(t)
        assert rec_initial is not None

        out = svc.resolve("GW-X", "rebind", {
            "token": rec_initial.token,
            "network_id": existing.id,
        })

        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], "conflict")
        self.assertEqual(set(out["conflict_fields"]), {"freq_hz", "sync_word"})
        # gateway_mac was still stamped — the conflict tells the
        # operator they need to resolve the RF mismatch next.
        self.assertEqual(existing.gateway_mac, "GW-X")


class FirstContactAdoptionTests(unittest.TestCase):
    """A bound network with ``rf_config=None`` (v1→v2 default before
    any gateway ever spoke) silently adopts the gateway's reported
    config — no operator wizard needed."""

    def test_first_contact_silent_adopt(self):
        ctrl, _gw, svc, broadcasts, persists = _make_service(
            gw=_FakeGatewayService(configs={"GW-A": _GATEWAY_CFG_MATCH}),
        )
        net = RL_Network(name="Default", gateway_mac="GW-A", rf_config=None)
        ctrl.network_repository.append(net)
        t = _FakeTransport("GW-A", network_id=net.id)
        ctrl._transports.append(t)

        rec = svc.evaluate(t)

        assert rec is not None
        self.assertEqual(rec.state, BindState.BOUND)
        self.assertEqual(net.rf_config, _GATEWAY_CFG_MATCH)
        # Persistence was triggered for the silent adopt.
        self.assertTrue(persists)
        self.assertEqual(broadcasts[-1][0], GatewayBindService.EVENT_BOUND)


class TokenGatingTests(unittest.TestCase):
    """A stale wizard answer (token from a previous evaluate cycle)
    must be rejected so a re-attach doesn't get clobbered by a
    queued-up dialog click."""

    def test_resolve_rejects_stale_token(self):
        ctrl, _gw, svc, _broadcasts, _persists = _make_service(
            gw=_FakeGatewayService(configs={"GW-A": _GATEWAY_CFG_DIVERGE}),
        )
        net = RL_Network(name="Track A", gateway_mac="GW-A",
                         rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(net)
        t = _FakeTransport("GW-A", network_id=net.id)
        ctrl._transports.append(t)
        rec1 = svc.evaluate(t)
        assert rec1 is not None
        stale_token = rec1.token

        # Re-evaluate (e.g. gateway reconnected). New token issued.
        svc.evaluate(t)

        out = svc.resolve("GW-A", "accept_gateway", {"token": stale_token})
        self.assertFalse(out["ok"])
        self.assertIn("stale token", out["error"])
        # Network was not mutated.
        self.assertEqual(net.rf_config, _HOST_CFG)


class ForgetAndUnknownGatewayTests(unittest.TestCase):

    def test_forget_drops_record(self):
        ctrl, _gw, svc, _b, _p = _make_service(
            gw=_FakeGatewayService(configs={"GW-A": _GATEWAY_CFG_MATCH}),
        )
        net = RL_Network(name="Track A", gateway_mac="GW-A",
                         rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(net)
        t = _FakeTransport("GW-A", network_id=net.id)
        ctrl._transports.append(t)
        svc.evaluate(t)
        self.assertIsNotNone(svc.get("GW-A"))

        svc.forget("GW-A")
        self.assertIsNone(svc.get("GW-A"))

    def test_resolve_unknown_ident_mac_errors(self):
        _ctrl, _gw, svc, _b, _p = _make_service()
        out = svc.resolve("UNKNOWN-MAC", "accept_gateway", {})
        self.assertFalse(out["ok"])
        self.assertIn("unknown gateway", out["error"])

    def test_evaluate_without_ident_mac_returns_none(self):
        _ctrl, _gw, svc, broadcasts, _p = _make_service()
        t = _FakeTransport(ident_mac="")
        rec = svc.evaluate(t)
        self.assertIsNone(rec)
        # Nothing broadcast — we silently skip.
        self.assertEqual(broadcasts, [])


class SnapshotTests(unittest.TestCase):

    def test_snapshot_lists_every_record(self):
        ctrl, _gw, svc, _b, _p = _make_service(
            gw=_FakeGatewayService(configs={
                "GW-A": _GATEWAY_CFG_MATCH,
                "GW-B": _GATEWAY_CFG_DIVERGE,
            }),
        )
        net_a = RL_Network(name="Track A", gateway_mac="GW-A",
                           rf_config=dict(_HOST_CFG))
        net_b = RL_Network(name="Track B", gateway_mac="GW-B",
                           rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(net_a)
        ctrl.network_repository.append(net_b)
        t_a = _FakeTransport("GW-A", network_id=net_a.id)
        t_b = _FakeTransport("GW-B", network_id=net_b.id)
        ctrl._transports.extend([t_a, t_b])
        svc.evaluate(t_a)
        svc.evaluate(t_b)

        snap = svc.snapshot()

        idents = {row["ident_mac"] for row in snap["gateways"]}
        self.assertEqual(idents, {"GW-A", "GW-B"})
        states = {row["ident_mac"]: row["state"] for row in snap["gateways"]}
        self.assertEqual(states["GW-A"], "bound")
        self.assertEqual(states["GW-B"], "conflict")


class EvaluateNoReadbackPendingTests(unittest.TestCase):
    """Round 4 Task 4: when GET_RF_CONFIG returns nothing (timeout
    during reconnect-stress) the bind service must park the record at
    PENDING with NO conflict broadcast — pre-fix, ``_rf_diff`` against
    ``actual_cfg=None`` flipped state to CONFLICT and popped the
    wizard with "Gateway reports" cells all blank."""

    def test_no_readback_parks_at_pending_without_broadcast(self):
        ctrl, _gw, svc, broadcasts, _persists = _make_service(
            gw=_FakeGatewayService(configs={"GW-A": None}),
        )
        net = RL_Network(name="Track A", gateway_mac="GW-A",
                         rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(net)
        t = _FakeTransport("GW-A", network_id=net.id)
        ctrl._transports.append(t)

        rec = svc.evaluate(t)

        assert rec is not None
        self.assertEqual(rec.state, BindState.PENDING)
        self.assertEqual(rec.conflict_fields, [])
        # Critical: NO gateway_conflict broadcast — the WebUI wizard
        # would otherwise open with all "Gateway reports" cells blank.
        conflict_events = [
            ev for ev in broadcasts if ev[0] == GatewayBindService.EVENT_CONFLICT
        ]
        self.assertEqual(conflict_events, [])
        # And no spurious gateway_bound either.
        bound_events = [
            ev for ev in broadcasts if ev[0] == GatewayBindService.EVENT_BOUND
        ]
        self.assertEqual(bound_events, [])


class RetuneGatewayTests(unittest.TestCase):
    """``retune_gateway`` — the cheap conflict resolution: bring the
    gateway onto the network's config, touch no device."""

    def _conflicted(self, *, set_result=None):
        gw = _FakeGatewayService(
            configs={"GW-A": _GATEWAY_CFG_DIVERGE}, set_result=set_result,
        )
        ctrl, _gw, svc, broadcasts, persists = _make_service(gw=gw)
        net = RL_Network(name="Start", gateway_mac="GW-A",
                         rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(net)
        t = _FakeTransport("GW-A", network_id=net.id)
        ctrl._transports.append(t)
        rec = svc.evaluate(t)
        assert rec is not None
        self.assertEqual(rec.state, BindState.CONFLICT)
        return ctrl, gw, svc, net, rec, broadcasts, persists

    def test_pushes_network_config_to_gateway_and_binds(self):
        ctrl, gw, svc, net, rec, broadcasts, _persists = self._conflicted()

        out = svc.resolve("GW-A", "retune_gateway", {"token": rec.token})

        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], "bound")
        self.assertTrue(out["rebooting"])
        # Exactly one write, carrying the HOST's config, persisted.
        self.assertEqual(len(gw.set_calls), 1)
        self.assertEqual(gw.set_calls[0]["rf_config"], _HOST_CFG)
        self.assertTrue(gw.set_calls[0]["persist"])
        self.assertEqual(gw.set_calls[0]["ident_mac"], "GW-A")
        # The network is the authority here and must NOT have moved.
        self.assertEqual(net.rf_config, _HOST_CFG)
        rec_after = svc.get("GW-A")
        assert rec_after is not None
        self.assertEqual(rec_after.state, BindState.BOUND)
        self.assertEqual(rec_after.conflict_fields, [])
        self.assertEqual(rec_after.rf_config_actual, _HOST_CFG)
        self.assertEqual(broadcasts[-1][0], GatewayBindService.EVENT_BOUND)

    def test_does_not_touch_devices(self):
        """The whole point versus ``accept_host``: no migration task, so
        nothing can strand."""
        tasks = _FakeTaskManager()
        migration = _FakeRfMigrationService()
        gw = _FakeGatewayService(configs={"GW-A": _GATEWAY_CFG_DIVERGE})
        ctrl = _FakeController(task_manager=tasks, rf_migration_service=migration)
        _ctrl, _gw, svc, _b, _p = _make_service(ctrl=ctrl, gw=gw)
        net = RL_Network(name="Start", gateway_mac="GW-A",
                         rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(net)
        t = _FakeTransport("GW-A", network_id=net.id)
        ctrl._transports.append(t)
        rec = svc.evaluate(t)
        assert rec is not None

        out = svc.resolve("GW-A", "retune_gateway", {"token": rec.token})

        self.assertTrue(out["ok"])
        self.assertEqual(migration.calls, [])
        self.assertEqual(tasks.start_calls, [])

    def test_gateway_rejection_keeps_conflict(self):
        _ctrl, gw, svc, net, rec, _b, _p = self._conflicted(
            set_result={"ok": False, "reason_name": "RF_CHANGE_BAD_PARAM"},
        )

        out = svc.resolve("GW-A", "retune_gateway", {"token": rec.token})

        self.assertFalse(out["ok"])
        self.assertIn("RF_CHANGE_BAD_PARAM", out["error"])
        # State untouched, so the operator can pick another resolution.
        self.assertEqual(out["state"], "conflict")
        rec_after = svc.get("GW-A")
        assert rec_after is not None
        self.assertEqual(rec_after.state, BindState.CONFLICT)
        self.assertEqual(net.rf_config, _HOST_CFG)

    def test_requires_conflict_state(self):
        ctrl, _gw, svc, _b, _p = _make_service(
            gw=_FakeGatewayService(configs={"GW-A": _GATEWAY_CFG_MATCH}),
        )
        net = RL_Network(name="Start", gateway_mac="GW-A",
                         rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(net)
        t = _FakeTransport("GW-A", network_id=net.id)
        ctrl._transports.append(t)
        rec = svc.evaluate(t)
        assert rec is not None
        self.assertEqual(rec.state, BindState.BOUND)

        out = svc.resolve("GW-A", "retune_gateway", {"token": rec.token})

        self.assertFalse(out["ok"])
        self.assertIn("requires state=conflict", out["error"])

    def test_errors_when_network_has_no_rf_config(self):
        """Nothing to push. Must not fall back to adopting the gateway's
        config — that would silently become ``accept_gateway``."""
        gw = _FakeGatewayService(configs={"GW-A": _GATEWAY_CFG_DIVERGE})
        ctrl, _gw, svc, _b, _p = _make_service(gw=gw)
        net = RL_Network(name="Start", gateway_mac="GW-A",
                         rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(net)
        t = _FakeTransport("GW-A", network_id=net.id)
        ctrl._transports.append(t)
        rec = svc.evaluate(t)
        assert rec is not None
        # Drop the expectation after the conflict was raised.
        rec.rf_config_expected = None

        out = svc.resolve("GW-A", "retune_gateway", {"token": rec.token})

        self.assertFalse(out["ok"])
        self.assertIn("no rf_config to push", out["error"])
        self.assertEqual(gw.set_calls, [])

    def test_errors_when_transport_detached(self):
        ctrl, gw, svc, _net, rec, _b, _p = self._conflicted()
        ctrl._transports.clear()

        out = svc.resolve("GW-A", "retune_gateway", {"token": rec.token})

        self.assertFalse(out["ok"])
        self.assertIn("not attached", out["error"])
        self.assertEqual(gw.set_calls, [])


class ConflictReassignmentTests(unittest.TestCase):
    """A conflicted gateway may be re-purposed onto another network
    instead of forcing a win/lose choice on the current one."""

    def _conflicted(self):
        gw = _FakeGatewayService(configs={"GW-A": _GATEWAY_CFG_DIVERGE})
        ctrl, _gw, svc, broadcasts, persists = _make_service(gw=gw)
        start = RL_Network(name="Start", gateway_mac="GW-A",
                           rf_config=dict(_HOST_CFG))
        ctrl.network_repository.append(start)
        t = _FakeTransport("GW-A", network_id=start.id)
        ctrl._transports.append(t)
        rec = svc.evaluate(t)
        assert rec is not None
        self.assertEqual(rec.state, BindState.CONFLICT)
        return ctrl, svc, start, rec, broadcasts, persists

    def test_create_network_from_conflict_releases_the_old_one(self):
        ctrl, svc, start, rec, _b, _p = self._conflicted()

        out = svc.resolve("GW-A", "create_network",
                          {"token": rec.token, "name": "Test"})

        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], "bound")
        # The old network keeps its RF config (its devices never moved)
        # but no longer claims the gateway.
        self.assertEqual(start.rf_config, _HOST_CFG)
        self.assertIsNone(start.gateway_mac)
        # Exactly one network owns the ident now.
        owners = [
            n for n in ctrl.network_repository.list()
            if str(getattr(n, "gateway_mac", "") or "") == "GW-A"
        ]
        self.assertEqual(len(owners), 1)
        self.assertEqual(owners[0].name, "Test")
        # The new network is seeded from what the gateway actually runs.
        self.assertEqual(owners[0].rf_config, _GATEWAY_CFG_DIVERGE)

    def test_rebind_from_conflict_moves_the_ident(self):
        ctrl, svc, start, rec, _b, _p = self._conflicted()
        other = RL_Network(name="Track", gateway_mac=None,
                           rf_config=dict(_GATEWAY_CFG_DIVERGE))
        ctrl.network_repository.append(other)

        out = svc.resolve("GW-A", "rebind",
                          {"token": rec.token, "network_id": other.id})

        self.assertTrue(out["ok"])
        # Matching config on the target -> straight to BOUND.
        self.assertEqual(out["state"], "bound")
        self.assertEqual(other.gateway_mac, "GW-A")
        self.assertIsNone(start.gateway_mac)
        self.assertEqual(start.rf_config, _HOST_CFG)


class NetworkDeviceImpactTests(unittest.TestCase):
    """Master-persistence visibility: which devices would go deaf if this
    network's gateway changed."""

    def test_reports_only_pinned_devices_of_that_network(self):
        from racelink.domain.node_config import MAC_FILTER_PERSIST_BIT

        devices = [
            _FakeDevice("AA", network_id="net-1",
                        configByte=MAC_FILTER_PERSIST_BIT, last_seen_ts=123.0),
            _FakeDevice("BB", network_id="net-1", configByte=0x01),
            _FakeDevice("CC", network_id="net-2",
                        configByte=MAC_FILTER_PERSIST_BIT),
        ]
        ctrl = _FakeController(devices=devices)
        _ctrl, _gw, svc, _b, _p = _make_service(ctrl=ctrl)

        impact = svc.network_device_impact("net-1")

        self.assertEqual(impact["total"], 2)
        self.assertEqual([d["mac"] for d in impact["master_persist"]], ["AA"])
        self.assertEqual(impact["master_persist"][0]["last_seen_ts"], 123.0)
        # AA was seen, so the reading is not flagged stale.
        self.assertFalse(impact["stale"])

    def test_never_seen_device_is_flagged_stale(self):
        """``configByte`` for a device that never reported is a stored
        guess; the UI has to say so rather than imply a live read."""
        from racelink.domain.node_config import MAC_FILTER_PERSIST_BIT

        ctrl = _FakeController(devices=[
            _FakeDevice("AA", network_id="net-1",
                        configByte=MAC_FILTER_PERSIST_BIT, last_seen_ts=0.0),
        ])
        _ctrl, _gw, svc, _b, _p = _make_service(ctrl=ctrl)

        impact = svc.network_device_impact("net-1")

        self.assertTrue(impact["stale"])
        self.assertEqual(len(impact["master_persist"]), 1)

    def test_empty_for_unknown_network(self):
        ctrl = _FakeController(devices=[_FakeDevice("AA", network_id="net-1")])
        _ctrl, _gw, svc, _b, _p = _make_service(ctrl=ctrl)

        impact = svc.network_device_impact("nope")

        self.assertEqual(impact["total"], 0)
        self.assertEqual(impact["master_persist"], [])
        self.assertFalse(impact["stale"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
