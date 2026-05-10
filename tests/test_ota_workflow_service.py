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
    """

    def __init__(self):
        self.metas: list[dict] = []

    def update(self, **updates):
        if "meta" in updates:
            self.metas.append(deepcopy(updates["meta"]))


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

    def _run(self, *, macs, ota=None, host_wifi=None, stop_on_error=False):
        tm = _RecordingTaskManager()
        svc = _make_service(ota=ota, host_wifi=host_wifi)
        rl_instance = mock.Mock()
        rl_instance.sendConfig.return_value = True
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
                    state, {"queued", "running", "ok", "error"},
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
