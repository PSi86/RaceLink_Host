"""Long-running OTA and presets workflows independent of Flask routes."""

from __future__ import annotations

import logging
import time
from typing import Any

# Diagnostic logger for the broad-except sweep (2026-04-27 cont.).
# Most error paths in this module accumulate ``results["errors"]`` for
# the operator-facing toast, which is good for visibility but loses
# the traceback. Adding a module logger lets us preserve the full
# stack for the inevitable "OTA failed but I don't know why" support
# session, without adding noise to the operator UI.
logger = logging.getLogger(__name__)


class OTAWorkflowService:
    def __init__(self, *, host_wifi_service, ota_service, presets_service):
        self.host_wifi = host_wifi_service
        self.ota = ota_service
        self.presets = presets_service

    def _restore_host_wifi(self, results, *, host_wifi_restore, host_wifi_initial, ssid):
        """Bring the WLED-AP connection down and turn the host's WiFi
        radio off (if we were the ones who turned it on). ``ssid`` is the
        SSID we connected to during the OTA — used as the NM connection id
        for the ``con down`` call. NM keeps the persistent profile so the
        next OTA reuses the stored secrets without re-prompting.

        ``profile_down`` (now ``disconnect_ap``) failures are surfaced as
        a non-fatal note in ``results["errors"]``: previously they were
        only debug-logged, which left the operator unaware that their
        normal WiFi might not have auto-reconnected. The radio-off step
        runs regardless so a stale connection doesn't keep the radio
        bound to the WLED AP.
        """
        if host_wifi_restore and (host_wifi_initial is False) and self.host_wifi.radio_enabled():
            try:
                try:
                    self.host_wifi.disconnect_ap(ssid, timeout_s=10.0)
                except Exception as ex:
                    # Surface as a soft warning in the toast — the radio
                    # turn-off below still recovers the operator's normal
                    # state, but the operator should know NM didn't
                    # cleanly release the AP.
                    results["errors"].append(
                        f"Host WiFi cleanup: disconnect from {ssid!r} failed: "
                        f"{str(ex) or type(ex).__name__}"
                    )
                    # Warning is the actionable single line; the
                    # traceback drops to DEBUG so support sessions can
                    # still pull it (logger config -> DEBUG) without
                    # spamming operators on the standard log level.
                    # Operator-actionable: the message already names
                    # the failure mode. No traceback (DEBUG or
                    # otherwise) — the stack frames don't add anything
                    # to a routine NM cleanup hiccup.
                    logger.warning("disconnect_ap(%r) failed during restore: %s", ssid, ex)
                self.host_wifi.set_radio(False)
                results["hostWifi"]["enabled"] = False
                results["hostWifi"]["restored"] = True
            except Exception as ex:
                # swallow-ok: surfaces via ``results["errors"]``. The
                # short prefix ("Host WiFi restore failed: ") tells the
                # operator which phase broke; the exception text follows
                # cleanly without the Python class name (2026-05-19).
                results["errors"].append(
                    f"Host WiFi restore failed: {str(ex) or type(ex).__name__}"
                )
                results["ok"] = False
                logger.warning("host wifi restore failed: %s", ex)

    def _ensure_wifi_ready(self, task_manager, *, wifi, host_wifi_enable, host_wifi_initial, results, meta):
        host_wifi_changed = False
        if host_wifi_enable and not host_wifi_initial:
            task_manager.update(meta={**meta, "stage": "HOST_WIFI_ON", "message": "Enabling host WiFi radio..."})
            self.host_wifi.set_radio(True)
            host_wifi_changed = True
            self.host_wifi.wait_iface_ready(wifi["iface"], timeout_s=15.0)
            results["hostWifi"]["enabled"] = True
        return host_wifi_changed

    def _connect_wled_wifi(self, task_manager, *, wifi, host_wifi_enable, host_wifi_changed, results, meta, avoid_bssid: str = ""):
        """Scan for any of ``wifi["ssids"]`` and connect to the first
        match using ``wifi["password"]``. Returns ``(matched_ssid,
        host_wifi_changed)``: callers stash the SSID for the
        per-device result and the restore path's ``con down`` call.

        When no explicit ``wifi["bssid"]`` was supplied, predict the
        target device's SoftAP BSSID from its STA MAC (ESP32 default
        ``AP_MAC = STA_MAC + 1``). This locks the connect to the
        intended device's AP even when the previous device's AP is
        still in the scan cache with a stronger signal. Falls back
        to ``<auto>`` if the prediction fails (malformed MAC).

        ``avoid_bssid`` is the BSSID of the *previously* connected
        device — passed through to ``connect_ap`` so the multi-device
        fallback can pick a single non-avoid candidate when the
        predicted BSSID isn't visible (handles WLED nodes that don't
        follow the ESP32 ``AP_MAC = STA_MAC + 1`` default).
        """
        ssids = list(wifi["ssids"])
        ssids_label = ", ".join(ssids) if len(ssids) > 1 else (ssids[0] if ssids else "<none>")
        # bssid hint: operator-supplied wins; otherwise derive from
        # this iteration's target MAC carried in ``meta["addr"]``.
        bssid_hint = str(wifi.get("bssid") or "").strip()
        if not bssid_hint:
            addr = str(meta.get("addr") or "")
            if addr:
                bssid_hint = self.ota.expected_softap_bssid(addr)
        task_manager.update(
            meta={
                **meta,
                "stage": "CONNECT_WIFI",
                "message": f'Connecting host WiFi (iface {wifi["iface"]}) to SSID "{ssids_label}"',
            }
        )
        try:
            matched = self.host_wifi.connect_ap(
                ssids,
                wifi["password"],
                iface=wifi["iface"],
                bssid=bssid_hint,
                avoid_bssid=avoid_bssid,
                timeout_s=wifi["timeout_s"],
            )
            return matched, host_wifi_changed
        except Exception as ex:
            message = str(ex)
            if host_wifi_enable and (not host_wifi_changed) and ("Wi-Fi is disabled" in message or "wireless is disabled" in message.lower()):
                task_manager.update(
                    meta={
                        **meta,
                        "stage": "HOST_WIFI_ON",
                        "message": f'Host WiFi appears disabled; enabling on {wifi["iface"]}...',
                    }
                )
                self.host_wifi.set_radio(True)
                results["hostWifi"]["enabled"] = True
                self.host_wifi.wait_iface_ready(wifi["iface"], timeout_s=15.0)
                matched = self.host_wifi.connect_ap(
                    ssids,
                    wifi["password"],
                    iface=wifi["iface"],
                    bssid=bssid_hint,
                    avoid_bssid=avoid_bssid,
                    timeout_s=wifi["timeout_s"],
                )
                return matched, True
            raise

    def download_presets(self, *, rl_instance, task_manager, mac: str, base_url: str, wifi: dict, host_wifi_enable: bool, host_wifi_restore: bool):
        results = {
            "ok": True, "baseUrl": base_url, "addr": mac, "file": None, "errors": [],
            "cancelled": False,
        }
        host_wifi_initial = self.host_wifi.radio_enabled()
        results["hostWifi"] = {"wasEnabled": host_wifi_initial, "enabled": host_wifi_initial, "restored": False}
        # ``connected_ssid`` is the SSID we actually associated to so the
        # restore path can deactivate it cleanly. Captured below from
        # ``_connect_wled_wifi`` and consumed by ``_restore_host_wifi``.
        connected_ssid = ""

        try:
            # Cooperative cancel: check before any host-WiFi mutation. If
            # the operator hits Cancel before we have changed network
            # state there is nothing to roll back.
            if task_manager.is_cancel_requested():
                results["cancelled"] = True
                logger.info("presets-download cancelled by operator before WiFi setup")
                return results
            host_wifi_changed = self._ensure_wifi_ready(
                task_manager,
                wifi=wifi,
                host_wifi_enable=host_wifi_enable,
                host_wifi_initial=host_wifi_initial,
                results=results,
                meta={"addr": mac},
            )

            task_manager.update(meta={"stage": "RACELINK_AP_ON", "addr": mac, "message": "Enable WLED AP via RaceLink (waiting for ACK)"})
            ok_ap = rl_instance.sendConfig(0x04, data0=1, recv3=self.ota.recv3_bytes_from_addr(mac), wait_for_ack=True, timeout_s=8.0)
            if not ok_ap:
                raise RuntimeError(f"Timeout waiting for CONFIG ACK from {mac}")

            connected_ssid, host_wifi_changed = self._connect_wled_wifi(
                task_manager,
                wifi=wifi,
                host_wifi_enable=host_wifi_enable,
                host_wifi_changed=host_wifi_changed,
                results=results,
                meta={"addr": mac},
            )

            # Second cancel check: WiFi setup is the only step where the
            # connect can take 5-10 s. After it lands we're on the device
            # AP, so cancel from here on means "skip the HTTP GET and
            # let the finally restore WiFi". The download itself is fast
            # (< 5 s) and not interrupted mid-flight.
            if task_manager.is_cancel_requested():
                results["cancelled"] = True
                logger.info("presets-download cancelled by operator after WiFi connect")
                return results

            expected_mac = self.ota.expected_mac_hex(mac)
            task_manager.update(meta={"stage": "WAIT_HTTP", "addr": mac, "message": f"Waiting for WLED /json/info mac to match {expected_mac}"})
            info = self.ota.wait_for_expected_node(base_url, expected_mac, timeout_s=90.0, poll_s=1.0)
            if not info:
                raise RuntimeError(f"Timeout waiting for node (baseUrl={base_url}) to report expected mac {expected_mac}")

            task_manager.update(meta={"stage": "DOWNLOAD_PRESETS", "addr": mac, "message": "Downloading presets.json"})
            payload = self.ota.wled_download_presets(base_url, timeout_s=15.0)
            saved = self.presets.save_payload(payload)
            results["file"] = {k: saved[k] for k in ("name", "size", "saved_ts")}
            results["files"] = self.presets.list_files()

            try:
                rl_instance.sendConfig(0x04, data0=0, recv3=self.ota.recv3_bytes_from_addr(mac), wait_for_ack=True, timeout_s=6.0)
            except Exception as ex:
                # swallow-ok: post-presets sendConfig is a "best to do
                # this but the workflow is already done" cleanup step.
                # Single-line debug entry so a stuck-state pattern is
                # still diagnosable without dumping a stack.
                logger.debug("post-presets sendConfig failed for %s: %s", mac, ex)
        except Exception as ex:
            # swallow-ok: surfaces via ``results["errors"]``. WARNING
            # carries the operator-actionable message; we deliberately
            # do NOT print the traceback even at DEBUG — the formatted
            # exception text says everything and a fleet OTA can hit
            # the same expected failure for several devices.
            results["ok"] = False
            results["errors"].append(str(ex) or type(ex).__name__)
            logger.warning("presets workflow failed for %s: %s", mac, ex)
        finally:
            self._restore_host_wifi(
                results,
                host_wifi_restore=host_wifi_restore,
                host_wifi_initial=host_wifi_initial,
                ssid=connected_ssid,
            )

        return results

    def run_firmware_update(
        self,
        *,
        rl_instance,
        task_manager,
        devices_provider,
        macs: list,
        base_url: str,
        fw_info=None,
        presets_info=None,
        cfg_info=None,
        retries: int = 3,
        stop_on_error: bool = False,
        wifi: dict,
        host_wifi_enable: bool,
        host_wifi_restore: bool,
        skip_validation: bool = False,
    ):
        results = {
            "ok": True,
            "baseUrl": base_url,
            "devices": [],
            "errors": [],
            # Cancel-aware fields populated by the cooperative cancel
            # check at the device-loop entry. ``cancelled_after`` is the
            # 1-based index of the last device that ran to completion
            # (success or per-device error); zero when cancel landed
            # before the first device. The WiFi-restore finally still
            # runs unconditionally — see ``_restore_host_wifi``.
            "cancelled": False,
            "cancelled_after": None,
        }
        host_wifi_initial = self.host_wifi.radio_enabled()
        results["hostWifi"] = {"wasEnabled": host_wifi_initial, "enabled": host_wifi_initial, "restored": False}
        # Captured from the most recent successful ``_connect_wled_wifi``
        # call so the finally-block restore knows which NM connection to
        # bring down. Re-set on each device so a multi-device run that
        # connects to nodes broadcasting different SSIDs (mixed-firmware
        # fleet) still releases the right one at the end.
        last_connected_ssid = ""
        # BSSID of the previous device we connected to. Threaded into
        # the next iteration's ``connect_ap`` as ``avoid_bssid`` so
        # the multi-device fallback can pick a single non-avoid
        # candidate when the predicted BSSID isn't visible (handles
        # WLED nodes that don't follow ``AP_MAC = STA_MAC + 1``).
        # Empty before the first device — no discriminator yet.
        last_connected_bssid = ""
        # Emit a single workflow-start line so an operator following the
        # log can confirm what was actually scheduled. Without this the
        # only signal a silently-skipped upload leaves is "no error but
        # no firmware change" — which is exactly the failure mode that
        # prompted the /update endpoint fix.
        ops = []
        if fw_info: ops.append(f"firmware ({fw_info['name']}, {fw_info['size']} B)")
        if presets_info: ops.append(f"presets ({presets_info['name']})")
        if cfg_info: ops.append(f"cfg ({cfg_info['name']})")
        logger.info(
            "fw-update workflow: %d device(s) %s, ops=%s",
            len(macs), [str(m) for m in macs],
            ", ".join(ops) if ops else "<none>",
        )
        if not ops:
            # Defensive: the API route should already 400 on this,
            # but if a future caller bypasses that guard we want a
            # loud failure rather than a silent "no work done" run
            # that the operator notices only after the fact.
            raise RuntimeError(
                "firmware-update called with no operations selected "
                "(no fw_info / presets_info / cfg_info); the API route "
                "should have rejected this with 400"
            )
        if fw_info:
            results["fw"] = {k: fw_info[k] for k in ("id", "name", "size", "sha256")}
        if presets_info:
            results["presets"] = {k: presets_info[k] for k in ("name", "size", "sha256")}
        if cfg_info:
            results["cfg"] = {k: cfg_info[k] for k in ("id", "name", "size", "sha256")}

        total = len(macs)
        # Authoritative per-device row state for the WebUI's progress
        # panel. Mutated in place across the per-device loop; every
        # ``emit()`` snapshots the latest values into the task meta so
        # ``FwProgressPanel`` can render row state directly instead of
        # heuristically inferring "everyone before addr is ok". See
        # frontend/POST_MIGRATION_CLEANUP.md §9 for the prior heuristic.
        addrs = [str(m) for m in macs]
        device_state: dict[str, str] = {a: "queued" for a in addrs}
        # Per-device live message companion to ``device_state``. Only
        # populated on the ``error`` transition so the WebUI row can
        # show the concrete failure ("Timeout waiting for CONFIG ACK
        # …") instead of the generic "error" label. Read by
        # FwProgressPanel via ``meta.deviceMessages`` before the task
        # ends — until 2026-05-19 the panel could only surface error
        # detail post-run from ``result.errors[]``.
        device_messages: dict[str, str] = {}

        def _meta_base(**extras: Any) -> dict[str, Any]:
            """Build a fresh meta dict carrying the workflow-wide fields
            plus a point-in-time snapshot of ``device_state``.

            The shallow copy is important: SSE broadcasts queue payload
            references, not serialised bytes, so a slow client reading
            the queue later would otherwise see a future mutation
            aliased into an earlier event.
            """
            return {
                "macs": addrs,
                "total": total,
                "retries": retries,
                "baseUrl": base_url,
                "deviceState": dict(device_state),
                "deviceMessages": dict(device_messages),
                **extras,
            }

        def emit(stage: str, **extras: Any) -> None:
            task_manager.update(meta=_meta_base(stage=stage, **extras))

        try:
            self._ensure_wifi_ready(
                task_manager,
                wifi=wifi,
                host_wifi_enable=host_wifi_enable,
                host_wifi_initial=host_wifi_initial,
                results=results,
                meta=_meta_base(index=0, addr=None),
            )

            for idx, addr in enumerate(macs, start=1):
                # Cooperative cancel: "after the current device" semantics.
                # Checked only at loop entry — the per-device flash + verify
                # + reconnect sequence below runs to completion once it has
                # started, so a cancelled OTA never leaves a device in a
                # half-flashed state. WiFi-restore still runs in the outer
                # finally regardless.
                if task_manager.is_cancel_requested():
                    results["cancelled"] = True
                    results["cancelled_after"] = idx - 1
                    logger.info(
                        "fw-update cancelled by operator after %d of %d device(s)",
                        idx - 1, total,
                    )
                    break
                addr_key = str(addr)
                device_state[addr_key] = "running"
                expected_mac = self.ota.expected_mac_hex(addr_key)
                dev_res = {
                    "addr": addr,
                    "expectedMac": expected_mac,
                    "groupId": self.ota.lookup_group_id_for_addr(addr_key, devices_provider()),
                    "ok": False,
                    "error": None,
                }
                results["devices"].append(dev_res)
                # Tracks whether the device has actually ACKed the
                # AP-enable. Read in the finally block to decide if
                # AP-Close needs to run as a cleanup step (only when
                # AP was opened but the subsequent upload failed —
                # otherwise the WLED reboot drops the AP for us, or
                # the AP never came up).
                ap_opened = False
                try:
                    # N2 setup: drop any stale IDENTIFY_REPLY event for
                    # this MAC so the post-AP-Close wait below only
                    # resolves on a *new* identify (after the reboot
                    # we're about to trigger).
                    _gw = getattr(rl_instance, "gateway_service", None)
                    if _gw is not None:
                        try:
                            _gw.clear_identify(str(addr))
                        except Exception as ex:
                            # swallow-ok: the wait is best-effort
                            # synchronisation; on failure we proceed
                            # without it and the next iteration may
                            # see the previous identify as fresh.
                            logger.debug("clear_identify failed for %s: %s", addr, ex)
                    # W4: wait for the device to ACK the AP-enable before
                    # starting the WiFi scan/connect — otherwise the host
                    # races into an empty scan list when LoRa latency
                    # delays the device's AP bring-up.
                    #
                    # 2026-05-19: switched from a single 8 s attempt to
                    # 1.5 s × 2 attempts (1 retry). Failed devices now
                    # surface in the UI within ~3 s instead of 8 s, and
                    # healthy devices typically ACK in < 1 s anyway. The
                    # retry helps if the first frame is lost in the radio
                    # without paying the legacy 8 s penalty.
                    ok_ap = False
                    for ap_attempt in range(1, 3):
                        emit(
                            "RACELINK_AP_ON",
                            index=idx, addr=addr, attempt=ap_attempt,
                            message=(
                                f"Enable WLED AP via RaceLink "
                                f"(try {ap_attempt}/2)"
                            ),
                        )
                        ok_ap = rl_instance.sendConfig(
                            0x04, data0=1,
                            recv3=self.ota.recv3_bytes_from_addr(addr_key),
                            wait_for_ack=True, timeout_s=1.5,
                        )
                        if ok_ap:
                            break
                    if not ok_ap:
                        raise RuntimeError(
                            f"Timeout waiting for CONFIG ACK from {addr} (AP-enable)"
                        )
                    # AP is now live on the device. From here on, any
                    # failure path MUST close the AP — see the finally
                    # block below. A clean success path doesn't need
                    # AP-Close: the WLED reboot triggered by the
                    # firmware-upload drops the AP automatically.
                    ap_opened = True
                    logger.info("OTA %s: AP-enable ACK received, scanning for SSIDs", addr)
                    matched_ssid, _changed = self._connect_wled_wifi(
                        task_manager,
                        wifi=wifi,
                        host_wifi_enable=host_wifi_enable,
                        host_wifi_changed=results["hostWifi"]["enabled"] and not host_wifi_initial,
                        results=results,
                        meta=_meta_base(index=idx, addr=addr),
                        avoid_bssid=last_connected_bssid,
                    )
                    # Remember which BSSID nmcli actually associated with
                    # — used as the next iteration's ``avoid_bssid``. The
                    # query is best-effort: a failure here only loses the
                    # discriminator for the multi-device fallback path,
                    # which then re-degrades to the predicted-only mode.
                    try:
                        last_connected_bssid = self.host_wifi.active_bssid(wifi["iface"]) or last_connected_bssid
                    except Exception as ex:
                        # swallow-ok: see comment above; logged once for
                        # support, no traceback (the nmcli error is the
                        # actionable signal, not the Python frames).
                        logger.debug(
                            "active_bssid lookup failed after connect for %s: %s",
                            addr, ex,
                        )
                    last_connected_ssid = matched_ssid or last_connected_ssid
                    dev_res["ssid"] = matched_ssid
                    logger.info("OTA %s: connected to SSID %r", addr, matched_ssid)
                    emit(
                        "WAIT_HTTP",
                        index=idx, addr=addr,
                        message=f"Waiting for WLED /json/info mac to match {expected_mac}",
                    )
                    info = self.ota.wait_for_expected_node(base_url, expected_mac, timeout_s=90.0, poll_s=1.0)
                    if not info:
                        raise RuntimeError(f"Timeout waiting for node (baseUrl={base_url}) to report expected mac {expected_mac}")
                    dev_res["info_before"] = {k: info.get(k) for k in ("mac", "ver", "arch", "name")}
                    logger.info(
                        "OTA %s: WLED reachable (mac=%s ver=%s name=%r)",
                        addr, info.get("mac"), info.get("ver"), info.get("name"),
                    )

                    if presets_info:
                        self.ota.wled_upload_file(base_url, presets_info["path"], timeout_s=45.0, dest_name="presets.json")
                    if cfg_info:
                        self.ota.wled_upload_file(base_url, cfg_info["path"], timeout_s=45.0, dest_name="cfg.json")
                    if fw_info:
                        ok = False
                        last_err = None
                        for attempt in range(1, retries + 1):
                            try:
                                emit(
                                    "UPLOAD_FW",
                                    index=idx, addr=addr,
                                    attempt=attempt,
                                    message=f"Uploading firmware (try {attempt}/{retries})",
                                )
                                # 60 s reflects the real ESP flash + reboot
                                # cycle better than the legacy 30 s default;
                                # the retry loop still bounds total time.
                                # ``ota_password`` is the WLED OTA password
                                # used by ``wled_upload_firmware``'s 401
                                # auto-unlock fallback; default is WLED's
                                # stock ``"wledota"``. ``skip_validation``
                                # lets the operator bypass WLED's
                                # release-name check for cross-fork
                                # migrations (off by default).
                                self.ota.wled_upload_firmware(
                                    base_url, fw_info["path"],
                                    timeout_s=60.0,
                                    ota_password=wifi.get("ota_password", "wledota"),
                                    skip_validation=skip_validation,
                                )
                                ok = True
                                break
                            except Exception as ex:
                                # swallow-ok: retry loop. ``last_err``
                                # surfaces in the RuntimeError below if
                                # all attempts fail; single-line debug
                                # entry naming the exception is enough
                                # to track intermittent failures across
                                # attempts. No traceback — the
                                # exception's message already carries
                                # the failure mode (HTTP 401, timeout,
                                # WLED rejected the binary, …) and
                                # multiplying it by N attempts × M
                                # devices makes the log unreadable.
                                last_err = ex
                                logger.debug(
                                    "wled_upload_firmware attempt %d/%d failed for %s: %s",
                                    attempt, retries, addr, ex,
                                )
                                # Bail fast on deterministic device-side
                                # OTA failures. HTTP 500 = WLED's
                                # ``Update.write()`` failed (release-name
                                # mismatch, partition layout, chip
                                # variant, bad CRC, …); HTTP 503 = WLED
                                # busy / aborting after a previous
                                # 500. Retrying without changing
                                # parameters (the firmware binary, the
                                # ``skipValidation`` flag, etc.) just
                                # delays the failure by ``retries × 2``
                                # seconds.
                                err_msg = str(ex)
                                if "HTTP 500" in err_msg or "HTTP 503" in err_msg:
                                    break
                                time.sleep(2.0)
                        if not ok:
                            raise RuntimeError(f"Firmware upload failed: {last_err}")
                    dev_res["ok"] = True
                    device_state[addr_key] = "ok"
                    emit(
                        "DEVICE_DONE",
                        index=idx, addr=addr,
                        message=f"{addr}: update complete",
                    )
                    logger.info("OTA %s: completed successfully", addr)
                    # Post-upload: hard-disconnect the host's WiFi from
                    # the now-rebooting device. ``-w 0`` skips the 802.11
                    # deactivation handshake (there is no peer to
                    # complete it — the device is in reset) and unblocks
                    # NM's scan-throttle so the *next* iteration's
                    # ``connect_ap`` doesn't spend ~10 s waiting for the
                    # scan cache to refresh. See neu 96.txt for the
                    # specific symptom this addresses.
                    try:
                        self.host_wifi.disconnect_iface_fast(wifi["iface"])
                    except Exception as ex:
                        # swallow-ok: post-upload disconnect is a
                        # performance optimisation, not load-bearing for
                        # correctness; the workflow finishes the device
                        # successfully either way.
                        logger.debug(
                            "post-upload disconnect_iface_fast failed for %s: %s",
                            addr, ex,
                        )
                except Exception as ex:
                    # Per-device failures are operator-actionable in the
                    # vast majority of cases (wrong password, polkit
                    # denied, WLED OTA lock / Same-network 401, …) and
                    # the formatted exception message already carries
                    # the diagnostic. WARNING is a single line; we
                    # deliberately do NOT print the traceback even at
                    # DEBUG, because RotorHazard typically runs at
                    # DEBUG level and a fleet OTA can hit the same
                    # expected failure mode for half the fleet.
                    #
                    # 2026-05-19: dropped the ``RuntimeError:`` prefix —
                    # the class name was Python-jargon that confused
                    # operators reading the WebUI summary. The fallback
                    # to ``type(ex).__name__`` only kicks in for
                    # exceptions whose ``__str__`` is empty (rare; some
                    # C-API errors). Same string is mirrored into
                    # ``device_messages`` so the live row can show it
                    # without waiting for the final result snapshot.
                    err_text = str(ex) or type(ex).__name__
                    dev_res["error"] = err_text
                    results["errors"].append(err_text)
                    device_state[addr_key] = "error"
                    device_messages[addr_key] = err_text
                    emit(
                        "DEVICE_ERROR",
                        index=idx, addr=addr,
                        message=f"{addr}: {err_text}",
                    )
                    logger.warning("fw upload failed for %s: %s", addr, ex)
                    if stop_on_error:
                        raise
                finally:
                    # N2 + N3 (re-ordered per neu 92.txt review):
                    # wait for the device to come back on the radio and
                    # for the standard auto-restore SET_GROUP to ACK
                    # BEFORE we send AP-Close. Otherwise the AP-Close
                    # frame goes out into the reboot window where the
                    # device cannot process it — neu 92.txt shows
                    # attempt 1/3 of every AP-Close timing out and only
                    # attempt 2/3 ACKing after the reboot.
                    #
                    # We only wait when the per-device upload actually
                    # succeeded — a failed device will not reboot, so
                    # IDENTIFY_REPLY will not arrive and we'd just burn
                    # 30 s here for nothing.
                    if dev_res.get("ok"):
                        gw = getattr(rl_instance, "gateway_service", None)
                        if gw is not None:
                            identify_ok = False
                            try:
                                emit(
                                    "REANNOUNCE_WAIT",
                                    index=idx, addr=addr,
                                    message=(
                                        f"Waiting for {addr} to re-register "
                                        "on RaceLink radio after reboot"
                                    ),
                                )
                                identify_ok = gw.wait_for_identify(
                                    str(addr), timeout_s=30.0
                                )
                            except Exception as ex:
                                # swallow-ok: best-effort synchronisation;
                                # on failure we fall through to the AP-
                                # Close + next iteration just like the
                                # legacy code did.
                                logger.debug(
                                    "wait_for_identify raised for %s: %s",
                                    addr, ex,
                                )
                            if not identify_ok:
                                logger.warning(
                                    "OTA %s: no IDENTIFY_REPLY within 30s "
                                    "after upload; auto-restore may not "
                                    "have run for this device",
                                    addr,
                                )
                                device_state[addr_key] = "reannounce_timeout"
                                dev_res["autoRestoreOk"] = "timeout"
                                # Snapshot the new state into the SSE
                                # stream — without an emit() here the
                                # last meta a client sees still carries
                                # the "ok" state from DEVICE_DONE.
                                emit(
                                    "REANNOUNCE_TIMEOUT",
                                    index=idx, addr=addr,
                                    message=(
                                        f"{addr} did not re-register on "
                                        "RaceLink within 30s"
                                    ),
                                )
                            else:
                                autorestore_ok = False
                                try:
                                    emit(
                                        "AUTORESTORE_WAIT",
                                        index=idx, addr=addr,
                                        message=(
                                            f"Waiting for auto-restore "
                                            f"SET_GROUP for {addr}"
                                        ),
                                    )
                                    autorestore_ok = gw.wait_for_auto_restore(
                                        str(addr), timeout_s=8.0
                                    )
                                except Exception as ex:
                                    # swallow-ok: see wait_for_identify
                                    # comment above.
                                    logger.debug(
                                        "wait_for_auto_restore raised for %s: %s",
                                        addr, ex,
                                    )
                                if not autorestore_ok:
                                    logger.warning(
                                        "OTA %s: auto-restore SET_GROUP did "
                                        "not finish within 8s; next device "
                                        "may collide on the radio",
                                        addr,
                                    )
                                    dev_res["autoRestoreOk"] = "timeout"
                                else:
                                    dev_res["autoRestoreOk"] = True
                    # N1: AP-Close with ACK. 2026-05-19: scoped to the
                    # *error-after-AP-open* case only. On a clean
                    # success the WLED reboot drops the AP for us; on
                    # an AP-enable timeout the AP never came up. But
                    # if AP-enable ACKed and a *later* step failed
                    # (wrong OTA password, bad firmware binary, HTTP
                    # 401/500/timeout, …) the device's AP is still
                    # broadcasting — and that's a soft-security
                    # concern, so we must close it before moving on.
                    #
                    # 1.5 s × 2 attempts matches AP-enable above:
                    # device is still alive on LoRa in this branch,
                    # so the first attempt almost always succeeds;
                    # the retry covers a single dropped frame.
                    if ap_opened and not dev_res.get("ok"):
                        try:
                            ap_closed = False
                            for close_attempt in range(1, 3):
                                emit(
                                    "AP_CLOSE",
                                    index=idx, addr=addr, attempt=close_attempt,
                                    message=(
                                        "Disable WLED AP via RaceLink "
                                        f"(try {close_attempt}/2)"
                                    ),
                                )
                                ap_closed = rl_instance.sendConfig(
                                    0x04, data0=0,
                                    recv3=self.ota.recv3_bytes_from_addr(str(addr)),
                                    wait_for_ack=True, timeout_s=1.5,
                                )
                                if ap_closed:
                                    break
                            if not ap_closed:
                                logger.warning(
                                    "OTA %s: AP-disable did not ACK within 2 × 1.5s "
                                    "(device may still be broadcasting its AP)",
                                    addr,
                                )
                        except Exception as ex:
                            # swallow-ok: per-device cleanup config send;
                            # workflow is already aborting. The next
                            # device's iteration won't be affected.
                            logger.debug("post-fw sendConfig failed for %s: %s", addr, ex)
        except Exception as ex:
            # swallow-ok: outer fallback for the per-device loop. The
            # ``stop_on_error=True`` raise above lands here; per-device
            # errors already populated results["errors"]. Single-line
            # WARNING with the exception message — no traceback,
            # matching the per-device path's quietness.
            results["ok"] = False
            logger.warning("firmware-update bulk aborted: %s", ex)
        finally:
            self._restore_host_wifi(
                results,
                host_wifi_restore=host_wifi_restore,
                host_wifi_initial=host_wifi_initial,
                ssid=last_connected_ssid,
            )

        # Workflow-end summary. The UI's status pill renders ``summary``
        # directly so the operator sees a single-line outcome instead of
        # the full per-device result JSON. The detailed result is also
        # emitted to the debug log so a support session can still pull
        # the full info without re-running the workflow.
        ok_count = sum(1 for d in results.get("devices", []) if d.get("ok"))
        total = len(addrs)
        err_count = len(results.get("errors", []))
        if results.get("cancelled"):
            after = results.get("cancelled_after") or 0
            summary = f"cancelled after {after}/{total} device(s)"
            if err_count:
                summary += f", {err_count} error(s)"
        elif err_count or ok_count < total:
            summary = f"{ok_count}/{total} ok, {err_count} error(s)"
        else:
            summary = f"{ok_count}/{total} ok"
        results["summary"] = summary
        logger.info("fw-update workflow finished: %s", summary)
        logger.debug("fw-update workflow full result: %s", results)

        return results
