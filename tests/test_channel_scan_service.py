"""Channel-scan service tests (Stage 3 Part F).

Pins:

  1. Per-channel volatile-switch + dwell + discover sequence.
  2. Restore-on-exit: the gateway is set back to its pre-scan RF
     config even when the scan body raised mid-channel.
  3. Known vs unknown responder partitioning: known devices get
     ``last_known_rf_config`` updated; unknown ones land in the
     scan's ``all_unknown`` list with their channel.
  4. Channel ID filtering (operator chose a subset).
  5. Validation paths: unknown gateway_id, unknown region, empty
     channel intersection, gateway that refuses to report its
     current RF config.
"""

from __future__ import annotations

import unittest

from racelink.domain.models import RL_Device, RL_Network
from racelink.domain.rf_channels import REGION_CHANNELS, channel_rf_config
from racelink.services.channel_scan_service import ChannelScanService


# A fake "currently persisted" gateway config — what the gateway is on
# before the scan starts and what it should be restored to at the end.
_PRE_SCAN_CFG = {
    "freq_hz":      866_500_000,
    "bw_khz_x10":   1250,
    "sf":           7,
    "cr_den":       5,
    "sync_word":    0x33,
    "tx_power_dbm": 14,
    "preamble":     8,
}


# ---- fakes ----------------------------------------------------------


class _FakeTransport:
    def __init__(self, ident_mac):
        self.ident_mac = ident_mac


class _FakeGatewayService:
    """Records set_gateway_rf_config + query_gateway_rf_config calls.

    ``responders_per_channel_id`` maps channel_id -> list of MACs that
    "answer" when the scan calls ``discovery_service.discover_devices``
    on that channel. The fake discovery service reads its current
    channel id from the gateway service's last-set config to decide
    which responder set to return.
    """

    def __init__(self, *, query_result=None, switch_result=None,
                 fail_channel_ids=None):
        self.query_result = query_result if query_result is not None else {
            "ok": True, "rf_config": dict(_PRE_SCAN_CFG),
        }
        self.switch_result = switch_result if switch_result is not None else {
            "ok": True, "reason": 0,
        }
        self.fail_channel_ids = set(fail_channel_ids or [])
        self.last_set_cfg: dict | None = None
        self.set_calls: list[dict] = []
        self.query_calls: list[dict] = []

    def query_gateway_rf_config(self, *, transport=None, timeout_s=0.5):
        self.query_calls.append({"transport_ident": getattr(transport, "ident_mac", None)})
        return dict(self.query_result)

    def set_gateway_rf_config(self, rf_config, *, persist=True, transport=None, timeout_s=1.0):
        call = {
            "rf_config": dict(rf_config),
            "persist": persist,
            "transport_ident": getattr(transport, "ident_mac", None),
        }
        self.set_calls.append(call)
        self.last_set_cfg = dict(rf_config)
        # If this is a per-channel switch and the caller pre-programmed
        # it to fail, surface a NACK.
        for ch in REGION_CHANNELS["EU868"]:
            if int(ch["freq_hz"]) == int(rf_config.get("freq_hz", -1)):
                if int(ch["id"]) in self.fail_channel_ids:
                    return {"ok": False, "reason": 4, "reason_name": "BAD_RANGE"}
                break
        return dict(self.switch_result)


class _FakeDiscoveryService:
    """Returns canned responders keyed by the channel the gateway is
    currently set to."""

    def __init__(self, gateway_service, responders_per_freq):
        self._gw = gateway_service
        # Map of freq_hz -> iterable of MAC strings
        self._responders = {int(k): list(v) for k, v in responders_per_freq.items()}
        self.calls: list[dict] = []

    def discover_devices(self, *, group_filter=255, target_device=None,
                         add_to_group=-1, transport=None):
        cfg = self._gw.last_set_cfg or {}
        freq = int(cfg.get("freq_hz", 0))
        responders = set(self._responders.get(freq, []))
        self.calls.append({
            "group_filter": group_filter,
            "freq": freq,
            "transport_ident": getattr(transport, "ident_mac", None),
            "responders": set(responders),
        })
        return {"found": len(responders), "responders": responders, "assigned_group": None}


class _FakeController:
    def __init__(self, *, devices, transports, gateway_service, discovery_service):
        self._devices = list(devices)
        self._transports = list(transports)
        self.gateway_service = gateway_service
        self.discovery_service = discovery_service
        self.persisted_calls: list[dict] = []

    @property
    def transports(self):
        return list(self._transports)

    def getDeviceFromAddress(self, mac):
        target = str(mac or "").upper()
        for d in self._devices:
            if str(getattr(d, "addr", "") or "").upper() == target:
                return d
        return None

    def save_to_db(self, args, scopes=None):
        self.persisted_calls.append({"args": dict(args or {}), "scopes": scopes})


def _device(mac, *, network_id="net-a"):
    d = RL_Device(mac, dev_type=0, name=mac)
    d.network_id = network_id
    d.link_online = True
    return d


def _make_service(*, devices=None, fail_channel_ids=None,
                  responders_per_freq=None, query_result=None):
    transport = _FakeTransport("GW-A")
    gw = _FakeGatewayService(
        query_result=query_result, fail_channel_ids=fail_channel_ids,
    )
    disc = _FakeDiscoveryService(gw, responders_per_freq or {})
    ctrl = _FakeController(
        devices=devices or [], transports=[transport],
        gateway_service=gw, discovery_service=disc,
    )
    svc = ChannelScanService(controller=ctrl)
    return ctrl, gw, disc, svc, transport


# Pre-resolve the EU868 channel rf-configs so we can address them by
# freq_hz in the responders map.
_EU = {int(ch["id"]): channel_rf_config("EU868", int(ch["id"]))
       for ch in REGION_CHANNELS["EU868"]}


class _no_sleep:
    """Patch only the channel-scan module's ``time.sleep`` reference."""

    def __enter__(self):
        from racelink.services import channel_scan_service as _mod
        import time as _time

        self._mod = _mod
        self._orig_time = _mod.time

        class _Shim:
            sleep = staticmethod(lambda _seconds: None)

            def __getattr__(self, name):
                return getattr(_time, name)

        _mod.time = _Shim()
        return self

    def __exit__(self, *_a):
        self._mod.time = self._orig_time


class HappyPathTests(unittest.TestCase):

    def test_scan_walks_every_channel_and_restores_at_end(self):
        # Two devices on the network — one will be seen on ch1, the
        # other on ch3. Channels 2/4/5 have no responders.
        d1 = _device("AABBCC111111")
        d2 = _device("AABBCC222222")
        responders = {
            _EU[1]["freq_hz"]: [d1.addr],
            _EU[3]["freq_hz"]: [d2.addr],
        }
        ctrl, gw, disc, svc, _t = _make_service(
            devices=[d1, d2], responders_per_freq=responders,
        )

        with _no_sleep():
            res = svc.scan_region("GW-A", "EU868")

        self.assertTrue(res["ok"])
        self.assertEqual(res["channels_scanned"], [1, 2, 3, 4, 5])

        # Per-channel partitioning: ch1 and ch3 each have one known.
        rows = {row["channel_id"]: row for row in res["channels_result"]}
        self.assertEqual(
            [r["mac"] for r in rows[1]["known"]],
            ["AABBCC111111"],
        )
        self.assertEqual(rows[1]["unknown"], [])
        self.assertEqual(
            [r["mac"] for r in rows[3]["known"]],
            ["AABBCC222222"],
        )
        # Cross-channel summary covers both responders.
        self.assertEqual(
            {e["mac"] for e in res["all_known"]},
            {d1.addr, d2.addr},
        )
        self.assertEqual(res["all_unknown"], [])
        self.assertEqual(res["summary"]["channels"], 5)
        self.assertEqual(res["summary"]["responders_total"], 2)
        self.assertEqual(res["summary"]["known_count"], 2)
        self.assertEqual(res["summary"]["unknown_count"], 0)

        # last_known_rf_config was updated for both devices.
        self.assertEqual(d1.last_known_rf_config, _EU[1])
        self.assertEqual(d2.last_known_rf_config, _EU[3])

        # Restore: final set call uses the snapshot config and persist=False.
        last_set = gw.set_calls[-1]
        self.assertEqual(last_set["rf_config"], _PRE_SCAN_CFG)
        self.assertFalse(last_set["persist"])
        # Persistence ran (we updated last_known on the devices).
        self.assertTrue(ctrl.persisted_calls)

        # Every discovery call routed through the right transport.
        for c in disc.calls:
            self.assertEqual(c["transport_ident"], "GW-A")
            self.assertEqual(c["group_filter"], 255)

    def test_unknown_responders_land_in_all_unknown(self):
        # Device on the host that the scan should NOT touch.
        known = _device("AABBCC111111")
        # Channel-3 responders are unknown MACs (not in the repo).
        responders = {
            _EU[1]["freq_hz"]: [known.addr],
            _EU[3]["freq_hz"]: ["DEADBEEF1111", "DEADBEEF2222"],
        }
        _ctrl, _gw, _disc, svc, _t = _make_service(
            devices=[known], responders_per_freq=responders,
        )

        with _no_sleep():
            res = svc.scan_region("GW-A", "EU868")

        self.assertTrue(res["ok"])
        unknowns = {e["mac"] for e in res["all_unknown"]}
        self.assertEqual(unknowns, {"DEADBEEF1111", "DEADBEEF2222"})
        # Channel-info preserved on each unknown entry.
        ch_ids = {e["channel_id"] for e in res["all_unknown"]}
        self.assertEqual(ch_ids, {3})


class ChannelFilterTests(unittest.TestCase):

    def test_operator_chosen_subset_only_scans_those(self):
        _ctrl, _gw, _disc, svc, _t = _make_service(devices=[])
        with _no_sleep():
            res = svc.scan_region("GW-A", "EU868", channel_ids=[2, 4])
        self.assertEqual(res["channels_scanned"], [2, 4])
        self.assertEqual([row["channel_id"] for row in res["channels_result"]],
                         [2, 4])

    def test_unknown_ids_dropped_silently(self):
        _ctrl, _gw, _disc, svc, _t = _make_service(devices=[])
        with _no_sleep():
            res = svc.scan_region("GW-A", "EU868", channel_ids=[1, 99])
        # Channel 99 doesn't exist; the scan still runs channel 1.
        self.assertEqual(res["channels_scanned"], [1])

    def test_empty_intersection_returns_error(self):
        _ctrl, _gw, _disc, svc, _t = _make_service(devices=[])
        with _no_sleep():
            res = svc.scan_region("GW-A", "EU868", channel_ids=[99, 100])
        self.assertFalse(res["ok"])
        self.assertIn("no matching channel ids", res["error"])


class FailureModeTests(unittest.TestCase):

    def test_per_channel_switch_failure_records_error_keeps_scanning(self):
        # Channel 2's volatile-switch fails with BAD_RANGE.
        _ctrl, _gw, _disc, svc, _t = _make_service(
            devices=[], fail_channel_ids={2},
        )
        with _no_sleep():
            res = svc.scan_region("GW-A", "EU868")
        rows = {row["channel_id"]: row for row in res["channels_result"]}
        self.assertIsNotNone(rows[2]["error"])
        self.assertIn("BAD_RANGE", rows[2]["error"])
        # Other channels still scanned.
        self.assertIsNone(rows[1]["error"])
        self.assertIsNone(rows[3]["error"])

    def test_unknown_gateway_id_returns_error(self):
        _ctrl, _gw, _disc, svc, _t = _make_service(devices=[])
        res = svc.scan_region("UNKNOWN-GW", "EU868")
        self.assertFalse(res["ok"])
        self.assertIn("no attached transport", res["error"])

    def test_unknown_region_returns_error(self):
        _ctrl, _gw, _disc, svc, _t = _make_service(devices=[])
        res = svc.scan_region("GW-A", "ZZ-FANTASY")
        self.assertFalse(res["ok"])
        self.assertIn("no channels", res["error"])

    def test_gateway_refuses_to_report_current_config(self):
        _ctrl, _gw, _disc, svc, _t = _make_service(
            devices=[],
            query_result={"ok": False, "error": "timeout"},
        )
        res = svc.scan_region("GW-A", "EU868")
        self.assertFalse(res["ok"])
        self.assertIn("current RF config", res["error"])
        # We refused to start so no per-channel calls happened.
        self.assertEqual(_gw.set_calls, [])

    def test_restore_runs_even_when_dwell_raises(self):
        # Simulate the discovery service raising on the second channel
        # — the scan should still restore at the end.
        d1 = _device("AABBCC111111")
        _ctrl, gw, disc, svc, _t = _make_service(devices=[d1])

        original_discover = disc.discover_devices
        calls_seen = []

        def _flaky_discover(**kwargs):
            calls_seen.append(kwargs)
            if len(calls_seen) == 2:
                raise RuntimeError("simulated transport hiccup")
            return original_discover(**kwargs)

        disc.discover_devices = _flaky_discover  # type: ignore[method-assign]

        with _no_sleep():
            res = svc.scan_region("GW-A", "EU868")

        # The second channel's row carries empty responders (the
        # ``except`` clause swallowed the raise), the scan didn't
        # abort, and the gateway was restored.
        self.assertEqual(res["channels_result"][1]["responders"], [])
        last_set = gw.set_calls[-1]
        self.assertEqual(last_set["rf_config"], _PRE_SCAN_CFG)
        self.assertFalse(last_set["persist"])


class ProgressCallbackTests(unittest.TestCase):

    def test_progress_emitted_for_switch_dwell_restore(self):
        _ctrl, _gw, _disc, svc, _t = _make_service(
            devices=[], responders_per_freq={},
        )
        progress: list[dict] = []
        with _no_sleep():
            svc.scan_region(
                "GW-A", "EU868", channel_ids=[1],
                progress_cb=lambda p: progress.append(p),
            )
        stages = [p["stage"] for p in progress]
        # At least one of each per channel + one restore at the end.
        self.assertIn("switch", stages)
        self.assertIn("dwell", stages)
        self.assertIn("restore", stages)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
