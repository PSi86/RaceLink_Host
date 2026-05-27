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

    # ----------------------------------------------------------------
    # Per-device / per-group migration (Stage 4 follow-up)
    # ----------------------------------------------------------------
    #
    # Unlike :meth:`migrate_network_to` which migrates a whole
    # network's worth of devices onto a new RF config (and reboots
    # the gateway), the next two methods MOVE a subset of devices
    # FROM their current network INTO an existing target network
    # whose gateway is already on the right RF config.
    #
    # Key differences vs migrate_network_to:
    #   * No gateway-RF mutation. Both source and target gateways stay
    #     on their persisted RF configs; we're flipping device
    #     membership, not network parameters.
    #   * Source transport for the wire push is the device's CURRENT
    #     network's gateway (which is still on the old config and can
    #     reach the device). The default ``transport_for_device(mac)``
    #     resolution in :meth:`GatewayService.set_node_rf_config`
    #     handles this automatically.
    #   * On success we flip ``device.network_id`` and
    #     ``last_known_rf_config`` so the host's bookkeeping matches
    #     where the device now physically lives.
    #
    # Offline-handling mirrors the ``_apply_device_meta_updates``
    # pattern for group moves: the host's metadata flips IMMEDIATELY
    # regardless of online state. The wire push is skipped for offline
    # devices in ``"skip"`` mode (operator's intent: "I'm reorganising;
    # offline can wait — Channel Scan recovers them") and attempted in
    # ``"force"`` mode (operator's intent: "try anyway, accept that
    # some may time out").
    #
    # The ``"block"`` mode is enforced at the API layer (HTTP 400
    # before the TaskManager job starts) so this service method never
    # actually sees it — by the time we get here the API has decided
    # which of ``skip`` / ``force`` to apply.

    def migrate_devices_to(
        self,
        target_network_id: str,
        macs: Iterable[str],
        *,
        offline_mode: str = "skip",
        progress_cb=None,
    ) -> dict:
        """Move a subset of devices from their current networks onto
        ``target_network_id``.

        Returns ``{"ok", "summary", "per_device", "stranded"}``. ``ok``
        is True iff every requested mac either pushed-OK or was
        skipped-already-on-target (offline-skip is counted as success
        — the metadata flip succeeded and Channel-Scan handles the
        physical reconciliation later).

        ``offline_mode``:
            * ``"skip"`` (default) — metadata flip only for offline
              devices; no wire push.
            * ``"force"`` — try the wire push even for offline
              devices (longer timeouts); metadata flip regardless.

        Group ``device.network_id`` flips happen INSIDE
        the state-repository lock (see :meth:`_state_lock`) — same
        discipline as ``_apply_device_meta_updates`` so the SSE
        reader thread never sees a half-updated device record.
        """
        target_net = self.controller.network_repository.get_by_id(target_network_id)
        if target_net is None:
            return {
                "ok": False,
                "stage": "validate",
                "error": f"unknown target network_id {target_network_id!r}",
                "summary": {},
                "per_device": [],
                "stranded": [],
            }
        target_rf = _normalize_rf_config(getattr(target_net, "rf_config", None))
        if target_rf is None:
            return {
                "ok": False,
                "stage": "validate",
                "error": (
                    f"target network {target_network_id!r} has no rf_config — "
                    "configure it via NetworkManager first"
                ),
                "summary": {},
                "per_device": [],
                "stranded": [],
            }

        # Coerce + dedupe the mac list while preserving operator-order
        # (the progress events fire in this order so the masterbar
        # progress matches the table row order in the UI).
        clean_macs: list = []
        seen: set = set()
        for raw in macs or ():
            mac = str(raw or "").strip().upper()
            if not mac or mac in seen:
                continue
            seen.add(mac)
            clean_macs.append(mac)

        # ---- Pre-check: partition the requested mac list ----------
        per_device: list = []
        push_set: list = []
        offline_set: list = []
        already_on_target: list = []
        not_found: list = []

        for mac in clean_macs:
            dev = self.controller.getDeviceFromAddress(mac)
            if dev is None:
                not_found.append(mac)
                per_device.append({"mac": mac, "status": "not_found"})
                continue
            current_net = str(getattr(dev, "network_id", "") or "")
            if current_net == str(target_network_id):
                already_on_target.append(mac)
                per_device.append({"mac": mac, "status": "already_on_target"})
                continue
            online = bool(getattr(dev, "link_online", False))
            if not online and offline_mode != "force":
                offline_set.append(mac)
            else:
                push_set.append(mac)

        result: dict = {
            "ok": False,
            "stage": "pre-check",
            "target_network_id": str(target_network_id),
            "per_device": per_device,
            "stranded": [],
            "summary": {
                "total_requested": len(clean_macs),
                "not_found": list(not_found),
                "already_on_target": list(already_on_target),
                "offline_skipped": list(offline_set),
                "push_count": len(push_set),
                "pushed_ok": 0,
                "pushed_fail": 0,
                "metadata_flipped": 0,
            },
        }
        self._emit_progress(
            progress_cb, stage="pre-check",
            index=0, total=len(push_set) + len(offline_set),
            push_count=len(push_set),
        )

        # ---- Offline metadata-only flips ---------------------------
        # Same shape as the group-move auto-restore path: update
        # ``device.network_id`` + ``last_known_rf_config`` so the host
        # bookkeeping reflects operator intent. Channel-Scan recovers
        # the device physically next time it's reachable.
        for idx, mac in enumerate(offline_set):
            self._emit_progress(
                progress_cb, stage="metadata-only",
                index=idx + 1, total=len(push_set) + len(offline_set),
                mac=mac,
            )
            self._apply_metadata_flip(
                mac, target_network_id, target_rf, online=False,
            )
            per_device.append({"mac": mac, "status": "offline_metadata_only"})
            result["summary"]["metadata_flipped"] += 1

        # ---- Online (and force-mode offline) wire pushes -----------
        # Flip-then-push ordering (Bug 4 fix, 2026-05-26): the device
        # reboots ~50 ms after the OPC_RF_CONFIG ACK and sends its
        # first IDENTIFY broadcast over the TARGET gateway within ~1 s
        # of boot. The auto-restore worker triggered by that IDENTIFY
        # routes the follow-up SET_GROUP via ``transport_for_device``
        # (= dev.network_id). If we flip network_id AFTER the wire-
        # push exhausts its 3-attempt timeout (~4.5 s when ACK gets
        # dropped because the node reboots faster than the LoRa TX
        # finishes), the IDENTIFY routes its SET_GROUP through the
        # OLD gateway — TX_REJECTED + 3× timeout. Flipping first +
        # passing the source transport explicitly to set_node_rf_config
        # closes the race without touching the WLED-side ACK timing.
        gw = self.gateway_service
        for idx, mac in enumerate(push_set):
            self._emit_progress(
                progress_cb, stage="device-migration",
                index=len(offline_set) + idx + 1,
                total=len(push_set) + len(offline_set),
                mac=mac,
            )
            # Pre-resolve the source transport BEFORE flipping
            # dev.network_id — ``transport_for_device`` reads that
            # field, so the order matters here.
            source_transport = None
            try:
                source_transport = self.controller.transport_for_device(mac)
            except Exception:
                # swallow-ok: routing helper is best-effort; fall back
                # to set_node_rf_config's own default resolution below.
                logger.debug(
                    "migrate_devices_to: transport_for_device raised for %s",
                    mac, exc_info=True,
                )

            # Flip metadata up-front so the post-reboot IDENTIFY routes
            # its SET_GROUP through the TARGET gateway. Same operator-
            # intent rule the offline-skip branch uses; we just hoist
            # it above the wire push so the race window is closed.
            self._apply_metadata_flip(
                mac, target_network_id, target_rf, online=True,
            )
            result["summary"]["metadata_flipped"] += 1

            # Mark offline so the UI tells the truth during the reboot
            # window and a late stale STATUS_REPLY from the old gateway
            # that slips past the cross-net guard can't flicker green.
            self._mark_offline_quietly(
                mac, reason="rf-migration in progress"
            )

            try:
                push_result = gw.set_node_rf_config(
                    mac, target_rf, transport=source_transport,
                )
            except Exception:
                logger.exception(
                    "RfMigrationService.migrate_devices_to: "
                    "set_node_rf_config raised for %s", mac,
                )
                push_result = {"ok": False, "error": "exception"}

            if push_result.get("ok"):
                per_device.append({"mac": mac, "status": "pushed"})
                result["summary"]["pushed_ok"] += 1
            else:
                # ACK-timeout is the expected post-reboot symptom (node
                # restarts faster than the LoRa TX queue can drain the
                # ACK on its way out). The metadata flip above already
                # set us up correctly; we only mark the device as
                # ``stranded`` for the operator-facing summary. The
                # automatic IDENTIFY → SET_GROUP path on the TARGET
                # gateway resolves the stranded state without operator
                # action.
                per_device.append({
                    "mac": mac,
                    "status": "push_failed",
                    "ack_status": push_result.get("ack_status"),
                    "error": push_result.get("error"),
                })
                result["summary"]["pushed_fail"] += 1
                result["stranded"].append(mac)

        # ---- Persist + done ---------------------------------------
        if result["summary"]["metadata_flipped"] > 0:
            self._persist_quietly(
                f"migrate_devices_to {target_network_id} "
                f"(flipped={result['summary']['metadata_flipped']})"
            )

        result["stage"] = "done"
        # ok == every requested mac landed in a non-error bucket. Push-
        # failures and not-found macs count as failure; offline-skip
        # and already-on-target count as success.
        result["ok"] = (
            result["summary"]["pushed_fail"] == 0
            and not not_found
        )
        return result

    def migrate_groups_to(
        self,
        target_network_id: str,
        group_ids: Iterable[int],
        *,
        offline_mode: str = "skip",
        progress_cb=None,
    ) -> dict:
        """Move every member device of each ``group_id`` onto
        ``target_network_id`` and flip each group's own ``network_id``.

        Single TaskManager-friendly job for the multi-group case: one
        progress stream, one persist call at the end, deduplicated
        member set so a device that lives in two of the requested
        groups (legitimate if the device.groupId only points to one
        but the operator picked both groups' worth of members) only
        gets one wire push.

        Group ``network_id`` flips happen regardless of partial
        member-migration failure — same operator-intent rule the
        single-group case used. Stragglers surface in
        ``result["stranded"]`` and recover via Channel-Scan.

        Returns the per-device shape from :meth:`migrate_devices_to`,
        extended with ``"group_ids"`` (the operator-supplied list)
        and ``"groups_flipped"`` (the subset that survived the
        validate stage).
        """
        target_net = self.controller.network_repository.get_by_id(target_network_id)
        if target_net is None:
            return {
                "ok": False,
                "stage": "validate",
                "error": f"unknown target network_id {target_network_id!r}",
                "group_ids": [int(g) for g in (group_ids or ())],
                "groups_flipped": [],
                "summary": {},
                "per_device": [],
                "stranded": [],
            }

        # Dedupe + coerce the group_ids list while preserving order.
        clean_gids: list = []
        seen_gids: set = set()
        for raw in (group_ids or ()):
            try:
                gid = int(raw) & 0xFF
            except (TypeError, ValueError):
                continue
            if gid in seen_gids:
                continue
            seen_gids.add(gid)
            clean_gids.append(gid)

        if not clean_gids:
            return {
                "ok": False,
                "stage": "validate",
                "error": "group_ids must be a non-empty list",
                "group_ids": [],
                "groups_flipped": [],
                "summary": {},
                "per_device": [],
                "stranded": [],
            }

        # Resolve groups + validate every id exists. Unknown ids are
        # rejected synchronously (mirrors the device-migration's
        # ``not_found`` handling) so the operator gets a fast 400.
        groups_list = list(self.controller.group_repository.list() or ())
        resolved_groups: list = []
        unknown_gids: list = []
        for gid in clean_gids:
            grp = groups_list[gid] if 0 <= gid < len(groups_list) else None
            if grp is None:
                unknown_gids.append(gid)
            else:
                resolved_groups.append((gid, grp))
        if unknown_gids:
            return {
                "ok": False,
                "stage": "validate",
                "error": f"unknown group_id(s): {unknown_gids}",
                "group_ids": clean_gids,
                "groups_flipped": [],
                "summary": {},
                "per_device": [],
                "stranded": [],
            }

        # Collect deduplicated member macs across all groups. A device
        # only has one groupId today, so dedup is just a safety net;
        # if a future model lets devices be in multiple groups the
        # set semantics already cover it.
        all_devices = list(self.controller.device_repository.list() or ())
        member_macs: list = []
        seen_macs: set = set()
        gid_set = {gid for gid, _ in resolved_groups}
        for dev in all_devices:
            if int(getattr(dev, "groupId", 0) or 0) not in gid_set:
                continue
            mac = str(getattr(dev, "addr", "") or "").upper()
            if not mac or mac in seen_macs:
                continue
            seen_macs.add(mac)
            member_macs.append(mac)

        result = self.migrate_devices_to(
            target_network_id, member_macs,
            offline_mode=offline_mode,
            progress_cb=progress_cb,
        )
        result["group_ids"] = clean_gids

        # Flip every requested group's network_id regardless of
        # partial member-migration failure. Operator-intent rule:
        # "this set of groups now belongs to network B".
        flipped: list = []
        with self._state_lock():
            for gid, grp in resolved_groups:
                try:
                    grp.network_id = str(target_network_id)
                    flipped.append(gid)
                except Exception:
                    # swallow-ok: stale repo entry / fake; the
                    # per-device flips already happened. We just
                    # don't claim this group's record was updated.
                    logger.exception(
                        "migrate_groups_to: failed to flip group.network_id for %d",
                        gid,
                    )
        result["groups_flipped"] = flipped
        self._persist_quietly(
            f"migrate_groups_to {target_network_id} groups={flipped}"
        )
        return result

    def _mark_offline_quietly(self, mac: str, *, reason: str) -> None:
        """Mark the device offline under the state-repository lock.
        Used by the wire-push loop to reflect the imminent reboot in
        the UI immediately; the cross-net guard in
        :meth:`GatewayService.on_transport_event` is the backstop for
        stale STATUS_REPLY frames the old gateway may still emit."""
        dev = self.controller.getDeviceFromAddress(mac)
        if dev is None:
            return
        with self._state_lock():
            try:
                dev.mark_offline(reason=reason)
            except Exception:
                # swallow-ok: fake/read-only device records have no
                # mark_offline; the migration shouldn't abort over a UI
                # convenience step.
                logger.debug(
                    "rf_migration: mark_offline raised for %s", mac,
                    exc_info=True,
                )

    def _apply_metadata_flip(
        self,
        mac: str,
        target_network_id: str,
        target_rf: dict,
        *,
        online: bool,
    ) -> None:
        """In-memory flip of ``device.network_id`` and
        ``last_known_rf_config``. Lock-scope is just the mutation —
        callers must NOT hold the lock across the wire send.

        ``online`` is informational only (logged); the metadata flip
        is the same in both cases.
        """
        dev = self.controller.getDeviceFromAddress(mac)
        if dev is None:
            return
        with self._state_lock():
            try:
                dev.network_id = str(target_network_id)
                dev.last_known_rf_config = dict(target_rf)
            except Exception:
                # swallow-ok: read-only fake / older record. The
                # operation logs and continues; the next save_to_db
                # picks up whatever attributes did mutate.
                logger.exception(
                    "migrate_devices_to: metadata flip raised for %s "
                    "(online=%s)", mac, online,
                )

    # ---- internals ----------------------------------------------------

    def _state_lock(self):
        """Return the state-repository mutation lock as a context
        manager, or a no-op fallback. Same shape as
        :meth:`GatewayService._state_lock` — the migration engine has
        the same locking contract as the service that serializes
        device repository writes against the RX-reader thread."""
        repo = getattr(self.controller, "state_repository", None)
        lock = getattr(repo, "lock", None) if repo is not None else None
        if lock is None:
            import contextlib
            return contextlib.nullcontext()
        return lock

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
