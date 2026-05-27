"""Single-Gateway Onboarding-Assistant service (Stage 1.5 part 2).

Orchestrates the three operator-guided repair flows that surface when a
host has only one gateway attached and existing devices need to be
brought back into sync with it.

Cases (operator picks one in the wizard):
    A) "Devices RF-compatible, only re-pair needed" -- Discover +
       SET_GROUP sweep over all (or selected) known devices. Exact fix
       for the 2026-05-20 bench observation where IDENTIFY_REPLY landed
       at the host but auto-restore was skipped because the device
       wasn't in the RAM repo (is_known_device=False gate). The
       explicit SET_GROUP send is what gets the WLED's
       learnMasterFromSender() called and unlocks future auto-restore.
    B) "Devices on old settings, migrate to new" -- The full
       "Devices ZUERST, Gateway DANACH" sequence:
         1. Gateway volatile-switch to OLD device settings.
         2. Discover devices on the old channel.
         3. Per-node OPC_RF_CONFIG push to NEW settings.
         4. Gateway persist-switch to NEW settings (reboots).
         5. Wait for reboot + auto-discovery, compare against the
            initial responder set to flag stranded devices.
    C) "Gateway -> device settings" (reverse direction) -- gateway
       switches to settings the devices already carry. No per-node
       push. Reboot triggers natural re-discovery.

The service is synchronous and returns a structured result dict; the
API layer wraps it in a TaskManager job so a long Case B (with reboot)
doesn't block the request thread.

Case D (settings unknown) is purely a UI hint -- no service entry
point. The wizard shows recovery options and points at the Channel
Scan that lands in Stage 3.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

from ..transport.framing import mac_last3_from_hex

logger = logging.getLogger(__name__)


# How long to wait for the gateway to come back after a persist-switch
# reboot in Case B / Case C. SX1262 init + USB-CDC enumeration on the
# ESP32-S3 takes ~2-3 s; we give a generous margin so the auto-discover
# pass after the wait actually finds the link.
_REBOOT_WAIT_S = 4.0


class OnboardingService:
    """Operator-driven repair flows for a single-gateway setup.

    Constructed with the live ``controller`` (which exposes the
    ``gateway_service`` and ``discovery_service`` attributes). All
    public methods return a result dict and never raise — the wrapping
    Task runner catches anything that slips through anyway.
    """

    def __init__(self, controller):
        self.controller = controller

    # ---- accessors --------------------------------------------------------

    @property
    def gateway_service(self):
        return self.controller.gateway_service

    @property
    def discovery_service(self):
        return self.controller.discovery_service

    # ---- shared helpers ---------------------------------------------------

    def _resolve_targets(self, target_macs: Optional[Iterable[str]]) -> list[str]:
        """Resolve the operator-supplied MAC list (or "all known") into a
        de-duplicated list of uppercase 12-char MACs that actually exist
        in the device repository.

        ``None`` / empty input falls back to every device in the repo.
        Unknown MACs are dropped silently — the wizard pre-populates from
        the repo so this should not happen in practice, but defensive
        filtering keeps the per-device result table honest.
        """
        repo_macs = {
            str(getattr(d, "addr", "") or "").upper()
            for d in self.controller.device_repository.list()
            if getattr(d, "addr", "")
        }
        if not target_macs:
            return sorted(repo_macs)
        out: list[str] = []
        for mac in target_macs:
            m = str(mac or "").strip().upper()
            if len(m) == 12 and m in repo_macs:
                out.append(m)
        # Preserve operator order; de-dupe.
        seen: set[str] = set()
        unique: list[str] = []
        for m in out:
            if m not in seen:
                unique.append(m)
                seen.add(m)
        return unique

    # ---- Case A: Re-pair --------------------------------------------------

    def case_a_re_pair(
        self,
        *,
        target_macs: Optional[Iterable[str]] = None,
        run_discover: bool = True,
        progress_cb=None,
    ) -> dict:
        """Re-pair known devices to the currently-attached gateway.

        Triggers a Discovery sweep on group=0 first (so the host's
        device_repository carries fresh online flags / RF telemetry)
        and then sends an explicit ``OPC_SET_GROUP`` per target, using
        the persisted ``device.groupId``. The receiving WLED calls
        ``learnMasterFromSender(persistIfEnabled=true)`` on every
        SET_GROUP frame, which re-binds it to this gateway.

        ``run_discover=False`` skips the discovery step (useful in
        tests where the device is mocked online already).
        """
        targets = self._resolve_targets(target_macs)
        result = {
            "ok": True,
            "case": "A",
            "stage": "starting",
            "per_device": [],
            "stranded": [],
            "summary": {"requested": len(targets), "succeeded": 0, "failed": 0},
        }
        if not targets:
            result["stage"] = "no-targets"
            return result

        # Phase 1 — Discover (best-effort; failure does not abort).
        if run_discover and self.discovery_service is not None:
            self._emit_progress(progress_cb, stage="discover", index=0, total=len(targets))
            try:
                self.discovery_service.discover_devices(group_filter=0)
            except Exception:
                logger.exception("onboarding case_a: discover_devices raised")

        # Phase 2 — per-device SET_GROUP.
        result["stage"] = "set-group"
        for idx, mac in enumerate(targets):
            self._emit_progress(progress_cb, stage="set-group", index=idx, total=len(targets), mac=mac)
            dev = self.controller.getDeviceFromAddress(mac)
            if dev is None:
                result["per_device"].append({"mac": mac, "status": "missing"})
                result["summary"]["failed"] += 1
                continue
            try:
                ok = bool(self.controller.setNodeGroupId(dev, forceSet=True, wait_for_ack=True))
            except Exception:
                logger.exception("onboarding case_a: setNodeGroupId raised for %s", mac)
                ok = False
            if ok:
                result["per_device"].append({"mac": mac, "status": "paired"})
                result["summary"]["succeeded"] += 1
            else:
                result["per_device"].append({"mac": mac, "status": "no_ack"})
                result["summary"]["failed"] += 1
                result["stranded"].append(mac)

        result["stage"] = "done"
        result["ok"] = result["summary"]["failed"] == 0
        return result

    # ---- Case B: Migrate to new settings ----------------------------------

    def case_b_migrate(
        self,
        *,
        old_rf_config: dict,
        new_rf_config: dict,
        target_macs: Optional[Iterable[str]] = None,
        progress_cb=None,
    ) -> dict:
        """Five-phase migration with the canonical "Devices ZUERST,
        Gateway DANACH" ordering.

        On any phase failure the gateway is left in whatever volatile
        state Phase 1 put it in; the operator's recovery path is a
        manual gateway-RF-reset via the existing Stage 1 dialog. The
        per-device push (Phase 3) is best-effort — a device that
        misses the push lands in ``stranded`` and the migration
        continues so the remaining nodes get their new config.
        """
        gateway = self.gateway_service
        targets = self._resolve_targets(target_macs)
        result = {
            "ok": False,
            "case": "B",
            "stage": "starting",
            "per_device": [],
            "stranded": [],
            "summary": {
                "requested": len(targets),
                "discovered": 0,
                "pushed_ok": 0,
                "pushed_fail": 0,
            },
        }

        # Phase 1 — Volatile gateway switch to OLD settings.
        self._emit_progress(progress_cb, stage="gw-volatile-old", index=0, total=5)
        result["stage"] = "gw-volatile-old"
        gw_old = gateway.set_gateway_rf_config(old_rf_config, persist=False)
        if not gw_old.get("ok"):
            result["error"] = f"gateway volatile-switch to old settings failed: {gw_old.get('reason_name') or gw_old.get('error')}"
            result["stage"] = "gw-volatile-old-failed"
            return result

        # Phase 2 — Discover on OLD channel.
        self._emit_progress(progress_cb, stage="discover-old", index=1, total=5)
        result["stage"] = "discover-old"
        discovered_macs: set[str] = set()
        try:
            disc = self.discovery_service.discover_devices(group_filter=0)
            for mac in disc.get("responders") or ():
                m = str(mac).strip().upper()
                if len(m) == 12:
                    discovered_macs.add(m)
        except Exception:
            logger.exception("onboarding case_b: discover_devices raised")
        result["summary"]["discovered"] = len(discovered_macs)

        # Operator may have pre-selected a subset; intersect with what
        # we actually saw on the wire so we don't push to absent nodes.
        push_set = (set(targets) & discovered_macs) if targets else discovered_macs

        # Phase 3 — Per-node OPC_RF_CONFIG push.
        self._emit_progress(progress_cb, stage="push", index=2, total=5)
        result["stage"] = "push"
        for idx, mac in enumerate(sorted(push_set)):
            self._emit_progress(progress_cb, stage="push", index=idx, total=len(push_set), mac=mac)
            try:
                push = gateway.set_node_rf_config(mac, new_rf_config)
            except Exception:
                logger.exception("onboarding case_b: set_node_rf_config raised for %s", mac)
                push = {"ok": False, "error": "exception"}
            if push.get("ok"):
                result["per_device"].append({"mac": mac, "status": "pushed"})
                result["summary"]["pushed_ok"] += 1
            else:
                result["per_device"].append({
                    "mac": mac,
                    "status": "push_failed",
                    "ack_status": push.get("ack_status"),
                    "error": push.get("error"),
                })
                result["summary"]["pushed_fail"] += 1

        # Targets that were never discovered are stranded straight
        # away — we don't know whether they're on the new channel
        # already (operator did half a migration earlier), powered
        # off, or just out of range. Channel-Scan from Stage 3 is the
        # recovery.
        if targets:
            for mac in targets:
                if mac not in discovered_macs:
                    result["stranded"].append(mac)

        # Phase 4 — Persist gateway switch to NEW settings. The gateway
        # ACKs the EV_RF_CHANGED then reboots; the host's existing
        # reconnect logic re-discovers the USB device.
        self._emit_progress(progress_cb, stage="gw-persist-new", index=3, total=5)
        result["stage"] = "gw-persist-new"
        gw_new = gateway.set_gateway_rf_config(new_rf_config, persist=True)
        if not gw_new.get("ok"):
            result["error"] = f"gateway persist-switch to new settings failed: {gw_new.get('reason_name') or gw_new.get('error')}"
            result["stage"] = "gw-persist-new-failed"
            return result

        # Phase 5 — Wait for the gateway to come back, then re-discover.
        self._emit_progress(progress_cb, stage="wait-reboot", index=4, total=5)
        result["stage"] = "wait-reboot"
        time.sleep(_REBOOT_WAIT_S)
        try:
            recheck = self.discovery_service.discover_devices(group_filter=0)
            seen_after = {
                str(m).upper() for m in (recheck.get("responders") or ()) if str(m)
            }
        except Exception:
            logger.exception("onboarding case_b: post-reboot discover_devices raised")
            seen_after = set()

        # Anything we pushed that didn't come back on the new channel
        # is stranded. Don't re-flag MACs already in stranded[].
        already_stranded = set(result["stranded"])
        for entry in result["per_device"]:
            mac = entry["mac"]
            if entry["status"] == "pushed" and mac not in seen_after and mac not in already_stranded:
                result["stranded"].append(mac)
                entry["status"] = "stranded"

        result["stage"] = "done"
        result["ok"] = (
            result["summary"]["pushed_fail"] == 0
            and not result["stranded"]
        )
        return result

    # ---- Case C: Gateway -> device settings -------------------------------

    def case_c_align_gateway(
        self,
        *,
        device_rf_config: dict,
        progress_cb=None,
    ) -> dict:
        """Gateway-only switch to the device-side settings.

        Used when the operator wants to keep all nodes on their
        current settings and just bring the gateway in line. After
        the persist-switch the gateway reboots; natural re-discovery
        + auto-restore re-pairs the nodes. The wizard pairs this with
        a follow-on Case A pass if the operator wants explicit
        SET_GROUP frames.
        """
        gateway = self.gateway_service
        result = {
            "ok": False,
            "case": "C",
            "stage": "starting",
            "per_device": [],
            "stranded": [],
            "summary": {},
        }
        self._emit_progress(progress_cb, stage="gw-persist", index=0, total=2)
        result["stage"] = "gw-persist"
        gw = gateway.set_gateway_rf_config(device_rf_config, persist=True)
        if not gw.get("ok"):
            result["error"] = f"gateway persist-switch failed: {gw.get('reason_name') or gw.get('error')}"
            result["stage"] = "gw-persist-failed"
            return result

        self._emit_progress(progress_cb, stage="wait-reboot", index=1, total=2)
        result["stage"] = "wait-reboot"
        time.sleep(_REBOOT_WAIT_S)
        try:
            disc = self.discovery_service.discover_devices(group_filter=0)
            result["summary"]["discovered"] = len(disc.get("responders") or ())
        except Exception:
            logger.exception("onboarding case_c: post-reboot discover_devices raised")
            result["summary"]["discovered"] = 0

        result["stage"] = "done"
        result["ok"] = True
        return result

    # ---- progress shim ----------------------------------------------------

    @staticmethod
    def _emit_progress(progress_cb, **fields) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(dict(fields))
        except Exception:
            # swallow-ok: progress is best-effort; a misbehaving
            # callback never aborts the migration.
            logger.debug("onboarding progress_cb raised", exc_info=True)
