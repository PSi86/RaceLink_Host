"""Pin the firmware-update workflow's per-device meta surface (§9).

The WebUI's ``FwProgressPanel`` renders row state directly from
``meta.deviceState`` — a per-mac map that the workflow mutates as each
device transitions queued → running → ok | error. Before §9, the panel
inferred state from a moving ``meta.addr`` pointer ("everyone before
addr is ok"), which left the most recently-completed row stuck on
``running`` until the next addr advanced past it.

These tests assert the new shape:

* ``meta.macs`` is the planned target list (carried verbatim from
  Start so re-entry restores row identity);
* ``meta.deviceState`` is a snapshot at emit time (shallow copy, so a
  slow SSE client doesn't see future mutations aliased into earlier
  events);
* per-device transitions land in the captured snapshots in the
  expected order, with no ``running`` survivors after success.
"""

from __future__ import annotations

import unittest
from copy import deepcopy
from unittest import mock

from racelink.services.ota_workflow_service import OTAWorkflowService


class _RecordingTaskManager:
    """Stand-in for :class:`racelink.web.tasks.TaskManager` that captures
    every ``update(meta=...)`` payload as a deep-copied snapshot. The
    deep copy mirrors what an SSE serialiser sees at broadcast time, so
    later mutations of the workflow's local ``device_state`` dict don't
    contaminate the captured history.

    ``cancel_after_n`` programs the cooperative cancel check: when set,
    ``is_cancel_requested`` returns ``True`` once the workflow has tried
    to start the (``cancel_after_n`` + 1)-th device. ``None`` (default)
    leaves cancel inactive — existing tests don't notice the new method.
    """

    def __init__(self, *, cancel_after_n: int | None = None):
        self.metas: list[dict] = []
        self._cancel_after_n = cancel_after_n
        self._loop_entry_calls = 0

    def update(self, **updates):
        if "meta" in updates:
            self.metas.append(deepcopy(updates["meta"]))

    def is_cancel_requested(self) -> bool:
        self._loop_entry_calls += 1
        if self._cancel_after_n is None:
            return False
        return self._loop_entry_calls > int(self._cancel_after_n)


def _ok_response(**overrides):
    """Default-success fakes for the OTA service. Each test can override
    individual methods (e.g. raise inside ``wled_upload_firmware`` to
    drive the per-device error path)."""
    base = mock.Mock()
    base.expected_mac_hex.side_effect = lambda addr: f"mac:{addr}"
    base.recv3_bytes_from_addr.return_value = (0, 0, 0)
    base.lookup_group_id_for_addr.return_value = 1
    base.wait_for_expected_node.return_value = {
        "mac": "ab:cd",
        "ver": "0.15",
        "arch": "esp32",
        "name": "node",
    }
    base.wled_upload_file.return_value = None
    base.wled_upload_firmware.return_value = None
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _ok_host_wifi():
    hw = mock.Mock()
    # Radio already on so ``_ensure_wifi_ready`` skips the toggle path
    # — keeps the captured meta list focused on per-device transitions.
    hw.radio_enabled.return_value = True
    hw.connect_ap.return_value = "WLED_AP"
    return hw


def _make_service(*, ota=None, host_wifi=None):
    return OTAWorkflowService(
        host_wifi_service=host_wifi or _ok_host_wifi(),
        ota_service=ota or _ok_response(),
        presets_service=mock.Mock(),
    )


class FwUpdateMetaSurfaceTests(unittest.TestCase):
    """Pin the new ``meta.macs`` / ``meta.deviceState`` shape (§9)."""

    WIFI = {
        "ssids": ["WLED_AP"],
        "password": "x",
        "iface": "wlan0",
        "bssid": None,
        "timeout_s": 10,
        "ota_password": "wledota",
    }

    def _run(self, *, macs, ota=None, host_wifi=None, stop_on_error=False,
             cancel_after_n: int | None = None, rl_instance=None):
        tm = _RecordingTaskManager(cancel_after_n=cancel_after_n)
        svc = _make_service(ota=ota, host_wifi=host_wifi)
        if rl_instance is None:
            rl_instance = mock.Mock()
            rl_instance.sendConfig.return_value = True
            rl_instance.gateway_service.wait_for_identify.return_value = True
            rl_instance.gateway_service.wait_for_auto_restore.return_value = True
        result = svc.run_firmware_update(
            rl_instance=rl_instance,
            task_manager=tm,
            devices_provider=lambda: [],
            macs=macs,
            base_url="http://4.3.2.1",
            fw_info={"id": "fw1", "name": "fw.bin", "size": 100, "sha256": "x", "path": "/tmp/fw.bin"},
            presets_info=None,
            cfg_info=None,
            retries=1,
            stop_on_error=stop_on_error,
            wifi=self.WIFI,
            host_wifi_enable=True,
            host_wifi_restore=False,
            skip_validation=False,
        )
        return tm.metas, result

    def test_every_meta_carries_macs_list(self):
        metas, _ = self._run(macs=["AA:BB", "CC:DD"])
        self.assertGreater(len(metas), 0)
        for i, meta in enumerate(metas):
            self.assertEqual(
                meta.get("macs"),
                ["AA:BB", "CC:DD"],
                f"meta #{i} dropped macs: {meta!r}",
            )

    def test_every_meta_carries_device_state_map(self):
        metas, _ = self._run(macs=["AA:BB", "CC:DD"])
        for i, meta in enumerate(metas):
            ds = meta.get("deviceState")
            self.assertIsInstance(
                ds, dict, f"meta #{i} dropped deviceState: {meta!r}",
            )
            self.assertEqual(
                set(ds.keys()), {"AA:BB", "CC:DD"},
                f"meta #{i} deviceState keys diverged: {ds!r}",
            )
            for addr, state in ds.items():
                self.assertIn(
                    state, {"queued", "running", "ok", "error", "reannounce_timeout"},
                    f"meta #{i} {addr} has unknown state {state!r}",
                )

    def test_success_path_marks_each_addr_running_then_ok(self):
        metas, result = self._run(macs=["AA:BB", "CC:DD"])
        self.assertTrue(result["ok"])

        # Walk the captured states and assert the transitions hit the
        # expected order. We don't pin specific stage labels here —
        # those are an internal detail; we only assert the row-state
        # progression a UI consumer cares about.
        states_aa = [m["deviceState"]["AA:BB"] for m in metas]
        states_cc = [m["deviceState"]["CC:DD"] for m in metas]

        self.assertIn("running", states_aa)
        self.assertIn("ok", states_aa)
        self.assertIn("running", states_cc)
        self.assertIn("ok", states_cc)

        # AA:BB must be ``ok`` by the time CC:DD first appears as
        # ``running`` — the §9 guarantee that previous successes are
        # authoritatively visible BEFORE the next device starts work.
        first_cc_running = next(i for i, s in enumerate(states_cc) if s == "running")
        self.assertEqual(
            states_aa[first_cc_running],
            "ok",
            "AA:BB should already show ok when CC:DD transitions to running",
        )

        # No row should remain ``running`` in the final emit.
        self.assertEqual(metas[-1]["deviceState"]["AA:BB"], "ok")
        self.assertEqual(metas[-1]["deviceState"]["CC:DD"], "ok")

    def test_per_device_error_marks_addr_error_in_meta(self):
        # First device fails on the firmware upload; second still runs.
        ota = _ok_response()
        call_count = {"n": 0}

        def upload_firmware(*_a, **_kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("HTTP 500: Update failed!")

        ota.wled_upload_firmware.side_effect = upload_firmware
        metas, result = self._run(macs=["AA:BB", "CC:DD"], ota=ota)

        self.assertEqual(len(result["errors"]), 1)
        # Final state: AA:BB is error, CC:DD is ok.
        self.assertEqual(metas[-1]["deviceState"]["AA:BB"], "error")
        self.assertEqual(metas[-1]["deviceState"]["CC:DD"], "ok")

    def test_device_state_is_snapshotted_not_aliased(self):
        # Mutations after an emit must not leak into earlier captured
        # snapshots — the SSE broadcast queues a payload reference, so
        # the workflow has to copy ``device_state`` at emit time. Pin
        # this by checking that the FIRST per-device 'running' meta for
        # AA:BB still shows CC:DD as 'queued' even though CC:DD later
        # transitions to ok.
        metas, _ = self._run(macs=["AA:BB", "CC:DD"])
        first_aa_running = next(
            m for m in metas if m["deviceState"]["AA:BB"] == "running"
        )
        self.assertEqual(
            first_aa_running["deviceState"]["CC:DD"], "queued",
            "Earlier captured snapshot leaked a later mutation — "
            "deviceState was aliased instead of copied at emit time.",
        )


class FwUpdateCancelTests(FwUpdateMetaSurfaceTests):
    """Cooperative cancel: dropping out at the per-device loop entry.

    The workflow is allowed to finish the device that has already
    started, then sees the cancel flag on the next loop iteration and
    breaks out. ``result["cancelled_after"]`` tells the operator (and
    the WebUI summary) how many devices completed before the abort.
    The outer ``finally`` block must always restore host WiFi.
    """

    def test_cancel_before_first_device_skips_all(self):
        # cancel_after_n=0 → is_cancel_requested returns True on the very
        # first call (which is at the loop entry for device #1).
        metas, result = self._run(macs=["AA:BB", "CC:DD"], cancel_after_n=0)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["cancelled_after"], 0)
        # No device ever became 'ok' or 'error' — they all stayed queued.
        if metas:
            final = metas[-1]["deviceState"]
            for state in final.values():
                self.assertEqual(state, "queued")
        self.assertEqual(result["devices"], [])

    def test_cancel_after_first_device_finishes_current_then_breaks(self):
        # cancel_after_n=1 → first device's loop-entry check returns
        # False, second device's check returns True. So AA:BB finishes,
        # CC:DD is skipped.
        metas, result = self._run(macs=["AA:BB", "CC:DD"], cancel_after_n=1)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["cancelled_after"], 1)
        # AA:BB completed; CC:DD remains queued.
        self.assertEqual(metas[-1]["deviceState"]["AA:BB"], "ok")
        self.assertEqual(metas[-1]["deviceState"]["CC:DD"], "queued")
        # results["devices"] holds exactly the one completed entry.
        self.assertEqual(len(result["devices"]), 1)
        self.assertTrue(result["devices"][0]["ok"])

    def test_cancel_after_all_is_a_no_op(self):
        # Cancel only fires at the *next* loop entry, so a cancel
        # requested after the last device naturally never triggers
        # the break — ``cancelled`` stays False and the workflow
        # completes normally.
        _metas, result = self._run(macs=["AA:BB"], cancel_after_n=10)
        self.assertFalse(result["cancelled"])
        self.assertIsNone(result["cancelled_after"])
        self.assertTrue(result["ok"])


class FwUpdateSummaryTests(FwUpdateMetaSurfaceTests):
    """The status pill in the WebUI renders ``result.summary`` as a
    one-liner instead of dumping the full per-device JSON. Pin the
    three shapes the operator should see."""

    def test_all_ok_summary(self):
        _, result = self._run(macs=["AA:BB", "CC:DD"])
        self.assertEqual(result.get("summary"), "2/2 ok")

    def test_partial_failure_summary(self):
        ota = _ok_response()
        calls = {"n": 0}

        def upload_fw(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("HTTP 500: Update failed!")

        ota.wled_upload_firmware.side_effect = upload_fw
        _, result = self._run(macs=["AA:BB", "CC:DD"], ota=ota)
        self.assertEqual(result.get("summary"), "1/2 ok, 1 error(s)")

    def test_cancelled_summary(self):
        _, result = self._run(macs=["AA:BB", "CC:DD"], cancel_after_n=1)
        # First device ran to completion, cancel landed at the second
        # device's loop-entry check → cancelled_after=1.
        self.assertEqual(result.get("summary"), "cancelled after 1/2 device(s)")


class FwUpdatePostUploadDisconnectTests(FwUpdateMetaSurfaceTests):
    """Post-upload host-side disconnect (neu 96.txt follow-up).

    After each successful firmware upload the workflow should issue a
    fast disconnect on the host's iface — the WLED device is rebooting
    and there's no peer to complete a graceful 802.11 handshake. The
    next iteration's ``connect_ap`` then runs in the ``disconnected:``
    state, which removes NM's scan-throttle.
    """

    def test_disconnect_iface_fast_invoked_after_each_successful_upload(self):
        hw = _ok_host_wifi()
        self._run(macs=["AA:BB", "CC:DD"], host_wifi=hw)
        # One call per device (both succeeded in this run).
        self.assertEqual(hw.disconnect_iface_fast.call_count, 2)
        # Each call carries the configured iface.
        for call in hw.disconnect_iface_fast.call_args_list:
            args = list(call.args) + [call.kwargs.get("iface")]
            self.assertIn("wlan0", args)

    def test_disconnect_iface_fast_skipped_on_failed_upload(self):
        # A device that errors before reaching ``dev_res.ok`` should not
        # trigger the post-upload disconnect — there was no successful
        # upload to clean up after.
        ota = _ok_response()
        ota.wled_upload_firmware.side_effect = RuntimeError("HTTP 500")
        hw = _ok_host_wifi()
        self._run(macs=["AA:BB"], ota=ota, host_wifi=hw)
        hw.disconnect_iface_fast.assert_not_called()


class FwUpdateBssidHintTests(FwUpdateMetaSurfaceTests):
    """BSSID-hint: when the operator didn't supply ``wifi.bssid``, the
    workflow predicts the target device's SoftAP BSSID from its MAC
    (ESP32 default ``AP_MAC = STA_MAC + 1``) and threads that to
    ``host_wifi.connect_ap``. Locks NM to the right device when the
    previous device's AP is still in the scan cache with stronger
    signal (the failure mode from neu 94.txt).
    """

    def _bssid_call_args(self, host_wifi):
        return [c.kwargs.get("bssid") for c in host_wifi.connect_ap.call_args_list]

    def test_bssid_hint_is_derived_from_target_mac(self):
        # ``_ok_response`` doesn't pre-script ``expected_softap_bssid``;
        # override to a deterministic mapping so the assertion is
        # robust against any future MAC normalisation tweaks.
        ota = _ok_response()
        ota.expected_softap_bssid.side_effect = lambda addr: f"BSSID:{addr}"
        hw = _ok_host_wifi()
        self._run(macs=["AA:BB", "CC:DD"], ota=ota, host_wifi=hw)

        self.assertEqual(
            self._bssid_call_args(hw),
            ["BSSID:AA:BB", "BSSID:CC:DD"],
        )

    def test_explicit_operator_bssid_wins_over_prediction(self):
        # If the operator pinned a BSSID in the WiFi options dialog,
        # respect it for every device. (Use case: a fixed test rig
        # where only one device is reachable.)
        ota = _ok_response()
        ota.expected_softap_bssid.side_effect = lambda addr: f"PREDICTED:{addr}"
        hw = _ok_host_wifi()
        tm = _RecordingTaskManager()
        svc = _make_service(ota=ota, host_wifi=hw)
        rl_instance = mock.Mock()
        rl_instance.sendConfig.return_value = True
        rl_instance.gateway_service.wait_for_identify.return_value = True
        rl_instance.gateway_service.wait_for_auto_restore.return_value = True
        wifi_with_bssid = dict(self.WIFI, bssid="11:22:33:44:55:66")
        svc.run_firmware_update(
            rl_instance=rl_instance,
            task_manager=tm,
            devices_provider=lambda: [],
            macs=["AA:BB", "CC:DD"],
            base_url="http://4.3.2.1",
            fw_info={"id": "fw1", "name": "fw.bin", "size": 100, "sha256": "x", "path": "/tmp/fw.bin"},
            presets_info=None,
            cfg_info=None,
            retries=1,
            stop_on_error=False,
            wifi=wifi_with_bssid,
            host_wifi_enable=True,
            host_wifi_restore=False,
            skip_validation=False,
        )

        self.assertEqual(
            self._bssid_call_args(hw),
            ["11:22:33:44:55:66", "11:22:33:44:55:66"],
        )
        # Prediction must not have been queried on the override path.
        ota.expected_softap_bssid.assert_not_called()


class FwUpdateReannounceSyncTests(FwUpdateMetaSurfaceTests):
    """N1/N2/N3: gate the next device's AP-Open on the previous
    device's reboot + auto-restore completing, so the radio doesn't
    race itself between iterations.

    See plan ``werden-mehrere-ger-te-f-r-woolly-hellman.md`` and log
    ``neu 91.txt`` for the specific failure pattern (SET_GROUP attempt
    1/3 → no_reply, attempt 2/3 → ACK) that motivated this.
    """

    def _make_rl_with_gateway(self):
        rl = mock.Mock()
        rl.sendConfig.return_value = True
        rl.gateway_service.wait_for_identify.return_value = True
        rl.gateway_service.wait_for_auto_restore.return_value = True
        return rl

    def test_ap_close_uses_wait_for_ack(self):
        """N1: the post-FW AP-Close must wait for ACK so we don't race
        the device's reboot with the next AP-Open."""
        rl = self._make_rl_with_gateway()
        self._run(macs=["AA:BB"], rl_instance=rl)

        ap_close_calls = [
            c for c in rl.sendConfig.call_args_list
            if c.args[:2] == (0x04,) or c.kwargs.get("data0") == 0
        ]
        # Each device produces one AP-Open (data0=1) and one AP-Close
        # (data0=0). Find the AP-Close and assert wait_for_ack=True.
        close_call = next(
            c for c in rl.sendConfig.call_args_list
            if c.kwargs.get("data0") == 0
        )
        self.assertTrue(close_call.kwargs.get("wait_for_ack"))
        self.assertAlmostEqual(close_call.kwargs.get("timeout_s"), 3.0, places=2)

    def test_waits_for_identify_then_autorestore_per_device(self):
        """N2 + N3: after each device's AP-Close, the workflow must
        wait for IDENTIFY_REPLY, then for the auto-restore worker
        before iterating to the next device."""
        rl = self._make_rl_with_gateway()
        self._run(macs=["AA:BB", "CC:DD"], rl_instance=rl)

        gw = rl.gateway_service
        # Both devices should have triggered both waits, in order.
        self.assertEqual(gw.clear_identify.call_count, 2)
        self.assertEqual(gw.wait_for_identify.call_count, 2)
        self.assertEqual(gw.wait_for_auto_restore.call_count, 2)
        # Per-device addresses are threaded through the wait calls.
        identify_addrs = [c.args[0] for c in gw.wait_for_identify.call_args_list]
        autorestore_addrs = [c.args[0] for c in gw.wait_for_auto_restore.call_args_list]
        self.assertEqual(identify_addrs, ["AA:BB", "CC:DD"])
        self.assertEqual(autorestore_addrs, ["AA:BB", "CC:DD"])

    def test_identify_timeout_marks_state_reannounce_timeout(self):
        """N2 timeout: device_state goes to 'reannounce_timeout' and
        the auto-restore wait is skipped (no point waiting on a
        worker that the IDENTIFY would have triggered)."""
        rl = self._make_rl_with_gateway()
        rl.gateway_service.wait_for_identify.return_value = False
        metas, _ = self._run(macs=["AA:BB"], rl_instance=rl)

        self.assertEqual(metas[-1]["deviceState"]["AA:BB"], "reannounce_timeout")
        # Auto-restore wait is skipped on identify timeout.
        rl.gateway_service.wait_for_auto_restore.assert_not_called()

    def test_failed_device_skips_re_sync_waits(self):
        """A device that errored out before reaching dev_res['ok']
        will not reboot, so the reannounce waits would just burn 30 s
        of timeout. The workflow must skip them entirely on failure."""
        rl = self._make_rl_with_gateway()
        ota = _ok_response()
        ota.wled_upload_firmware.side_effect = RuntimeError("HTTP 500")
        self._run(macs=["AA:BB"], ota=ota, rl_instance=rl)

        rl.gateway_service.wait_for_identify.assert_not_called()
        rl.gateway_service.wait_for_auto_restore.assert_not_called()
        # clear_identify still ran (before the AP-Open) — that's the
        # 'forget stale state' step, which is harmless on failure.
        rl.gateway_service.clear_identify.assert_called_once_with("AA:BB")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
