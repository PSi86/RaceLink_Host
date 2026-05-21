"""Multi-network RF migration engine (Stage 3 Part E).

Generalises the Stage-1.5 ``OnboardingService.case_b_migrate``
"Devices ZUERST, Gateway DANACH" pattern to multi-network setups.
Where Case B operates on the single attached gateway, this service:

  * targets one ``RL_Network`` at a time,
  * routes through the transport bound to that network's
    ``gateway_mac``,
  * filters devices by ``RL_Device.network_id`` so foreign-network
    devices are never touched,
  * skips devices whose ``last_known_rf_config`` already matches
    the target (no churn for already-on-target nodes),
  * uses :meth:`GatewayService.set_node_rf_config(transport=...)` so
    Stage-3 Part-C's gateway_id-required matcher contract is
    satisfied without bouncing the operator-visible bind state.

Phases (per the plan):

  1. **Pre-check** — enumerate devices on the network; partition
     into ``push`` (need migration), ``skipped_already_target``
     (already on the right RF), and ``offline`` (host believes
     them unreachable; the operator picks whether to proceed).
  2. **Phase 1 — Device migration** — per device, call
     ``set_node_rf_config(target)`` via the *current* gateway
     transport. Each device ACKs then reboots onto the new
     settings; the gateway can no longer see it until Phase 2
     completes. Progress is emitted per device.
  3. **Phase 2 — Gateway migration** — ``set_gateway_rf_config(
     target, persist=True)`` writes NVS and reboots the gateway.
     The host's existing reconnect machinery re-opens the USB
     device on the new settings.
  4. **Phase 3 — Verification** — discovery on the (now-new)
     channel; every device that pushed-OK but doesn't reply is
     marked ``stranded`` (Channel-Scan from Stage 3 Part F is the
     recovery). Successfully verified devices have their
     ``last_known_rf_config`` updated.

Bind-service integration: after Phase 2 the engine calls
``GatewayBindService.re_evaluate(ident_mac)`` so the ``conflict``/
``pending`` state flips to ``bound`` on the SSE channel.

Out of scope for Part E (Stage-3 follow-ups):

  * Persisted history per device (the plan mentions
    ``source="host_push"`` history entries). The result dict
    surfaces every per-device outcome but the host-side device
    record doesn't yet have a structured history field — adding
    one is a small follow-on patch.
  * Operator-driven override for stranded devices. The wizard
    surfaces them; the actual rescue is Part F's Channel Scan.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


# Reboot wait — mirrors the onboarding service constant. SX1262 init +
# USB-CDC enumeration on the gateway's ESP32-S3 takes ~2-3 s; 4 s is a
# safe upper bound.
_REBOOT_WAIT_S = 4.0


# P_RfConfig wire-format fields. The migration engine only considers
# these for the "already on target" equality check; helper fields like
# the channel-table's ``id`` / ``name`` are stripped at the boundary.
_RF_FIELDS = (
    "freq_hz",
    "bw_khz_x10",
    "sf",
    "cr_den",
    "sync_word",
    "tx_power_dbm",
    "preamble",
)


def _normalize_rf_config(cfg: Optional[dict]) -> Optional[dict]:
    """Return only the wire-format fields, int-coerced. ``None`` /
    incomplete inputs return ``None`` so equality checks short-circuit
    safely instead of comparing a partial dict against a full one."""
    if not isinstance(cfg, dict):
        return None
    out: dict = {}
    for f in _RF_FIELDS:
        if f not in cfg:
            return None
        try:
            out[f] = int(cfg[f])
        except (TypeError, ValueError):
            return None
    return out


def _rf_equal(a: Optional[dict], b: Optional[dict]) -> bool:
    na = _normalize_rf_config(a)
    nb = _normalize_rf_config(b)
    if na is None or nb is None:
        return False
    return all(na[f] == nb[f] for f in _RF_FIELDS)


class RfMigrationService:
    """Multi-network RF migration engine. Wired up by the controller
    next to the other services so the API layer and the bind service
    can call into it.
    """

    def __init__(self, controller, *, bind_service=None):
        self.controller = controller
        # The bind service is optional so tests can construct the
        # migration engine without spinning up the full controller.
        # The Stage-3 controller wiring sets it after both services
        # exist.
        self.bind_service = bind_service

    # ---- accessors ----------------------------------------------------

    @property
    def gateway_service(self):
        return self.controller.gateway_service

    @property
    def discovery_service(self):
        return self.controller.discovery_service

    def attach_bind_service(self, bind_service) -> None:
        self.bind_service = bind_service

    # ---- public surface ----------------------------------------------

    def migrate_network_to(
        self,
        network_id: str,
        target_rf_config: dict,
        *,
        force_offline: bool = False,
        progress_cb=None,
    ) -> dict:
        """Run the four-phase migration for ``network_id`` onto
        ``target_rf_config``. Returns a result dict; the host's
        TaskManager wrapper surfaces it through the SSE ``task``
        channel.

        ``force_offline`` (default ``False``): skip devices whose
        ``link_online`` flag is False. With ``True`` they're attempted
        anyway — useful when the operator knows a device just took a
        few seconds longer to come up after a previous migration.

        ``progress_cb({"stage": ..., "index": int, "total": int,
        "mac": str?})`` is invoked at every phase boundary and once
        per device push.
        """
        target = _normalize_rf_config(target_rf_config)
        if target is None:
            return {
                "ok": False,
                "stage": "validate",
                "error": "target_rf_config is missing wire-format fields",
                "summary": {},
            }

        network = self.controller.network_repository.get_by_id(network_id)
        if network is None:
            return {
                "ok": False,
                "stage": "validate",
                "error": f"unknown network_id {network_id!r}",
                "summary": {},
            }
        try:
            transport = self.controller.transport_for_network(network_id)
        except Exception:
            # swallow-ok: routing helper is best-effort; treat lookup
            # failure the same as "no transport bound".
            logger.exception(
                "RfMigrationService: transport_for_network raised for %s",
                network_id,
            )
            transport = None
        if transport is None:
            return {
                "ok": False,
                "stage": "validate",
                "error": (
                    f"network {network_id!r} has no attached transport — "
                    "plug the gateway in or bind it via the wizard first"
                ),
                "summary": {},
            }
        ident_mac = (getattr(transport, "ident_mac", "") or "").upper() or None

        # ---- Pre-check ------------------------------------------------
        # Snapshot the device list once; later phases iterate this
        # list, not the live repo, so a concurrent ``device_repository``
        # mutation doesn't smuggle a foreign-network device into the
        # push set mid-migration.
        try:
            all_devices = list(self.controller.device_repository.list())
        except Exception:
            logger.exception("RfMigrationService: device_repository.list raised")
            all_devices = []
        network_devices = [
            d
            for d in all_devices
            if str(getattr(d, "network_id", "") or "") == str(network_id)
        ]

        push_set: list = []
        skipped_already_target: list[str] = []
        skipped_offline: list[str] = []
        for dev in network_devices:
            mac = str(getattr(dev, "addr", "") or "").upper()
            if not mac:
                continue
            if _rf_equal(getattr(dev, "last_known_rf_config", None), target):
                skipped_already_target.append(mac)
                continue
            if not force_offline and not bool(getattr(dev, "link_online", False)):
                skipped_offline.append(mac)
                continue
            push_set.append(dev)

        result: dict = {
            "ok": False,
            "network_id": str(network_id),
            "ident_mac": ident_mac,
            "stage": "pre-check",
            "per_device": [],
            "stranded": [],
            "summary": {
                "total_devices_on_network": len(network_devices),
                "skipped_already_target": skipped_already_target,
                "skipped_offline": skipped_offline,
                "push_count": len(push_set),
                "pushed_ok": 0,
                "pushed_fail": 0,
                "verified": [],
            },
        }
        self._emit_progress(
            progress_cb, stage="pre-check",
            index=0, total=3 + len(push_set),
            push_count=len(push_set),
        )

        # ---- Phase 1 — Device migration -------------------------------
        result["stage"] = "device-migration"
        gw = self.gateway_service
        for idx, dev in enumerate(push_set):
            mac = str(getattr(dev, "addr", "") or "").upper()
            self._emit_progress(
                progress_cb, stage="device-migration",
                index=1 + idx, total=3 + len(push_set), mac=mac,
            )
            try:
                push_result = gw.set_node_rf_config(
                    mac, target, transport=transport,
                )
            except Exception:
                logger.exception(
                    "RfMigrationService: set_node_rf_config raised for %s",
                    mac,
                )
                push_result = {"ok": False, "error": "exception"}
            if push_result.get("ok"):
                result["per_device"].append({"mac": mac, "status": "pushed"})
                result["summary"]["pushed_ok"] += 1
            else:
                result["per_device"].append({
                    "mac": mac,
                    "status": "push_failed",
                    "ack_status": push_result.get("ack_status"),
                    "error": push_result.get("error"),
                })
                result["summary"]["pushed_fail"] += 1

        # ---- Phase 2 — Gateway migration ------------------------------
        result["stage"] = "gateway-migration"
        self._emit_progress(
            progress_cb, stage="gateway-migration",
            index=1 + len(push_set), total=3 + len(push_set),
        )
        gw_switch = gw.set_gateway_rf_config(
            target, persist=True, transport=transport,
        )
        if not gw_switch.get("ok"):
            result["stage"] = "gateway-migration-failed"
            result["error"] = (
                f"gateway persist-switch failed: "
                f"{gw_switch.get('reason_name') or gw_switch.get('error')}"
            )
            return result

        # The network now expects the new settings. Persist before the
        # reboot wait so a host crash mid-migration still produces a
        # consistent next-boot view.
        network.rf_config = dict(target)
        self._persist_quietly(f"migration on {network_id}")

        # ---- Wait for the gateway to come back ------------------------
        # The reboot drops the USB link; the controller's reconnect
        # path re-opens it. We sleep the canonical reboot window and
        # then trust the existing reconnect machinery.
        time.sleep(_REBOOT_WAIT_S)

        # ---- Phase 3 — Verification -----------------------------------
        result["stage"] = "verification"
        self._emit_progress(
            progress_cb, stage="verification",
            index=2 + len(push_set), total=3 + len(push_set),
        )
        seen_after: set[str] = set()
        try:
            recheck = self.discovery_service.discover_devices(group_filter=0)
            for mac in recheck.get("responders") or ():
                m = str(mac).strip().upper()
                if len(m) == 12:
                    seen_after.add(m)
        except Exception:
            logger.exception(
                "RfMigrationService: post-reboot discover_devices raised",
            )

        verified: list[str] = []
        for entry in result["per_device"]:
            mac = entry["mac"]
            if entry["status"] != "pushed":
                continue
            if mac in seen_after:
                entry["status"] = "verified"
                verified.append(mac)
                # Update the device's last_known_rf_config so the next
                # migration knows it's already on target.
                dev = self.controller.getDeviceFromAddress(mac)
                if dev is not None:
                    try:
                        dev.last_known_rf_config = dict(target)
                    except Exception:
                        # swallow-ok: read-only fake / older device
                        # record — non-critical to the migration's
                        # success.
                        logger.debug(
                            "RfMigrationService: could not stamp last_known_rf_config on %s",
                            mac, exc_info=True,
                        )
            else:
                entry["status"] = "stranded"
                result["stranded"].append(mac)
        result["summary"]["verified"] = verified
        if verified:
            self._persist_quietly(
                f"migration on {network_id} (verified={len(verified)})"
            )

        # ---- Tell the bind service we're done -------------------------
        # The bind service was likely sitting in ``conflict`` /
        # ``pending`` when the migration started; a re-evaluate now
        # finds the gateway on the new (matching) config and flips
        # the state to ``bound`` on the SSE channel.
        if ident_mac and self.bind_service is not None:
            try:
                self.bind_service.re_evaluate(ident_mac)
            except Exception:
                logger.exception(
                    "RfMigrationService: bind_service.re_evaluate raised for %s",
                    ident_mac,
                )

        result["stage"] = "done"
        result["ok"] = (
            result["summary"]["pushed_fail"] == 0
            and not result["stranded"]
        )
        return result

    # ---- internals ----------------------------------------------------

    def _persist_quietly(self, why: str) -> None:
        """Best-effort ``save_to_db`` so a mid-migration host crash
        doesn't leave the network's persisted ``rf_config`` out of
        sync with the gateway's NVS."""
        try:
            self.controller.save_to_db({}, scopes=None)
        except Exception:
            logger.exception(
                "RfMigrationService: save_to_db raised after %s", why,
            )

    @staticmethod
    def _emit_progress(progress_cb, **fields) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(dict(fields))
        except Exception:
            # swallow-ok: progress is best-effort; a misbehaving
            # callback never aborts the migration.
            logger.debug(
                "RfMigrationService progress_cb raised", exc_info=True,
            )
