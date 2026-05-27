"""Channel scan service for stranded-device recovery (Stage 3 Part F).

Operator scenario: a migration finished but some devices never came
back on the new channel (Part E's ``stranded`` list). The Channel
Scan service walks a region's channel table on one gateway,
temporarily switching its RF config per channel and listening for
``IDENTIFY_REPLY`` frames. Devices that respond on a channel are
matched against the local repository; known devices have their
``last_known_rf_config`` refreshed (so the next migration knows
where they are), unknown devices land in a "found" list with their
channel so the operator can decide whether to bind them.

Sequence per channel:

  1. Volatile-switch the gateway to the channel's RF config
     (``set_gateway_rf_config(channel.rf_config, persist=False)``).
     Volatile-switch means NVS stays on the operator's persisted
     config — a host crash mid-scan recovers cleanly on the next
     gateway reboot.
  2. Wait a short settle window (LoRa modem re-init).
  3. Broadcast ``OPC_DEVICES`` (groupId=255 wildcard) and dwell for
     ``identify_dwell_s`` seconds collecting IDENTIFY_REPLYs.
  4. Partition responders into known/unknown against the device
     repository; update ``last_known_rf_config`` on known ones.

At the end, volatile-switch back to the gateway's pre-scan RF
config so the gateway returns to the operator's intended channel
without needing a reboot.

The service is driven via the existing TaskManager so the WebUI
sees per-channel progress; a typical 5-channel scan with 2 s dwell
is ~15 s wall-clock.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

from ..domain.rf_channels import channel_rf_config, list_channels

logger = logging.getLogger(__name__)


# Wait between the volatile RF-config switch and the OPC_DEVICES
# broadcast. The SX1262 needs a few hundred ms to re-tune; 800 ms is
# generous without ballooning the per-channel cost.
_SETTLE_S = 0.8

# Default dwell — the operator can override via the API. Two seconds
# is roughly 2x the COLLECT_IDLE_TIMEOUT_S so any single late-comer
# gets a fair shake without blocking the scan unnecessarily.
DEFAULT_DWELL_S = 2.0


class ChannelScanService:
    """Walks a region's channel table and reports who's listening
    on each.

    Constructed once at controller startup; the TaskManager wraps
    every scan invocation so a long-running multi-channel sweep
    streams progress through the existing SSE ``task`` channel.
    """

    def __init__(self, controller):
        self.controller = controller

    # ---- accessors ----------------------------------------------------

    @property
    def gateway_service(self):
        return self.controller.gateway_service

    @property
    def discovery_service(self):
        return self.controller.discovery_service

    # ---- public surface ----------------------------------------------

    def scan_region(
        self,
        gateway_id: str,
        region: str,
        channel_ids: Optional[Iterable[int]] = None,
        *,
        identify_dwell_s: float = DEFAULT_DWELL_S,
        progress_cb=None,
    ) -> dict:
        """Scan ``channel_ids`` on the gateway identified by
        ``gateway_id`` (its ``ident_mac``).

        ``channel_ids=None`` scans every channel in ``region``. The
        return shape is::

            {
                "ok": bool,
                "gateway_id": ident_mac,
                "region": "EU868",
                "channels_scanned": [1, 2, ...],
                "channels_result": [
                    {
                        "channel_id": 1,
                        "channel_name": "Default",
                        "rf_config": {...},
                        "responders": ["AABB..."],
                        "known": [{"mac": ..., "name": ..., "network_id": ...}],
                        "unknown": [{"mac": ...}],
                        "error": str | None,
                    },
                    ...
                ],
                "all_known": [...],   # union across channels
                "all_unknown": [...], # union across channels
                "summary": {
                    "channels": int,
                    "responders_total": int,
                    "known_count": int,
                    "unknown_count": int,
                },
                "error": str | None,
            }

        The gateway is volatile-switched onto each channel for the
        duration of its dwell, then restored to the operator's
        pre-scan RF config. NVS is never touched.
        """
        result: dict = {
            "ok": False,
            "gateway_id": str(gateway_id or "").upper(),
            "region": str(region),
            "channels_scanned": [],
            "channels_result": [],
            "all_known": [],
            "all_unknown": [],
            "summary": {
                "channels": 0,
                "responders_total": 0,
                "known_count": 0,
                "unknown_count": 0,
            },
            "error": None,
        }

        # ---- Resolve the transport ------------------------------------
        transport = self._find_transport(gateway_id)
        if transport is None:
            result["error"] = (
                f"no attached transport carries ident_mac={gateway_id!r}"
            )
            return result

        # ---- Resolve the channel list ---------------------------------
        available = list_channels(region)
        if not available:
            result["error"] = f"region {region!r} has no channels"
            return result
        wanted_ids = (
            [int(c) for c in channel_ids]
            if channel_ids is not None
            else [int(ch["id"]) for ch in available]
        )
        by_id = {int(ch["id"]): ch for ch in available}
        # Filter to the operator-supplied subset, preserving order.
        channels = [by_id[i] for i in wanted_ids if i in by_id]
        if not channels:
            result["error"] = (
                f"no matching channel ids in region {region!r}: "
                f"requested={list(wanted_ids)} available={sorted(by_id.keys())}"
            )
            return result

        # ---- Snapshot the gateway's current RF config -----------------
        # We restore via volatile-switch at the end so the operator's
        # intended channel is what the gateway is broadcasting on
        # without paying for a reboot.
        original_rf = self._snapshot_gateway_rf(transport)
        if original_rf is None:
            result["error"] = (
                "could not read the gateway's current RF config "
                "(GW_CMD_GET_RF_CONFIG timed out)"
            )
            return result

        gw_service = self.gateway_service
        disc_service = self.discovery_service
        known_by_mac: dict[str, dict] = {}
        unknown_by_mac: dict[str, dict] = {}

        total = len(channels)
        try:
            for idx, ch in enumerate(channels):
                ch_id = int(ch["id"])
                ch_name = str(ch.get("name") or "")
                ch_cfg = channel_rf_config(region, ch_id) or {}
                self._emit_progress(
                    progress_cb, stage="switch",
                    index=idx, total=total, channel_id=ch_id, name=ch_name,
                )
                result["channels_scanned"].append(ch_id)
                ch_row: dict = {
                    "channel_id": ch_id,
                    "channel_name": ch_name,
                    "rf_config": dict(ch_cfg),
                    "responders": [],
                    "known": [],
                    "unknown": [],
                    "error": None,
                }
                result["channels_result"].append(ch_row)

                switch = gw_service.set_gateway_rf_config(
                    ch_cfg, persist=False, transport=transport,
                )
                if not switch.get("ok"):
                    ch_row["error"] = (
                        f"volatile-switch failed: "
                        f"{switch.get('reason_name') or switch.get('error')}"
                    )
                    continue

                # Modem re-tune settle.
                time.sleep(_SETTLE_S)

                self._emit_progress(
                    progress_cb, stage="dwell",
                    index=idx, total=total, channel_id=ch_id,
                    dwell_s=float(identify_dwell_s),
                )
                # Use the existing discovery helper — it builds a
                # wildcard matcher with an idle-timeout, which is the
                # exact "broadcast + dwell for late-comers" shape we
                # need. We override the max ceiling by sleeping AFTER
                # the call, but the matcher's own idle timeout
                # (COLLECT_IDLE_TIMEOUT_S, ~600 ms) gives the dwell
                # its early-exit behaviour.
                try:
                    disc = disc_service.discover_devices(
                        group_filter=255,
                        transport=transport,
                    )
                except Exception:
                    logger.exception(
                        "ChannelScanService: discover_devices raised on "
                        "ch%d (%s)", ch_id, ch_name,
                    )
                    disc = {"responders": set()}
                responders_raw = disc.get("responders") or set()

                # Defensive normalisation: the discovery service
                # already returns a set of 12-char uppercase MACs, but
                # any other shape (test fakes returning lists, etc.)
                # falls through to whatever str() produces.
                responders = sorted({
                    str(m).strip().upper()
                    for m in responders_raw
                    if isinstance(m, (str, bytes, bytearray))
                })
                ch_row["responders"] = responders

                for mac in responders:
                    if len(mac) != 12:
                        continue
                    dev = self.controller.getDeviceFromAddress(mac)
                    if dev is None:
                        entry = unknown_by_mac.setdefault(
                            mac, {"mac": mac, "channel_id": ch_id,
                                  "channel_name": ch_name},
                        )
                        ch_row["unknown"].append({"mac": mac})
                        continue
                    # Update last_known_rf_config so the next
                    # migration can skip this device (Part E's
                    # pre-check filter reads it).
                    try:
                        dev.last_known_rf_config = dict(ch_cfg)
                    except Exception:
                        # swallow-ok: read-only fake / older record;
                        # the scan result still informs the operator.
                        logger.debug(
                            "ChannelScanService: could not stamp "
                            "last_known_rf_config on %s", mac,
                            exc_info=True,
                        )
                    known_entry = {
                        "mac": mac,
                        "name": str(getattr(dev, "name", "") or ""),
                        "network_id": str(getattr(dev, "network_id", "") or "") or None,
                        "channel_id": ch_id,
                        "channel_name": ch_name,
                    }
                    known_by_mac.setdefault(mac, known_entry)
                    ch_row["known"].append(known_entry)
        finally:
            # Always attempt to restore the original config, even if the
            # scan raised mid-channel. If THIS fails the gateway is
            # left on whatever channel the last iteration set it to —
            # the operator's WebUI will see the channel mismatch via
            # the bind service's regular re-evaluate path on the next
            # connect/reconnect.
            self._emit_progress(progress_cb, stage="restore", index=total, total=total)
            try:
                gw_service.set_gateway_rf_config(
                    original_rf, persist=False, transport=transport,
                )
            except Exception:
                logger.exception(
                    "ChannelScanService: failed to restore original "
                    "RF config on %s",
                    getattr(transport, "ident_mac", "?"),
                )

        # ---- Aggregate results ---------------------------------------
        result["all_known"] = list(known_by_mac.values())
        result["all_unknown"] = list(unknown_by_mac.values())
        responders_total = sum(
            len(c["responders"]) for c in result["channels_result"]
        )
        result["summary"] = {
            "channels": total,
            "responders_total": responders_total,
            "known_count": len(known_by_mac),
            "unknown_count": len(unknown_by_mac),
        }
        # Persist the updated last_known_rf_config so a host restart
        # doesn't lose the scan's findings. Best-effort: a save failure
        # is logged but doesn't fail the scan.
        if known_by_mac:
            try:
                self.controller.save_to_db({}, scopes=None)
            except Exception:
                logger.exception(
                    "ChannelScanService: save_to_db raised after scan",
                )
        result["ok"] = True
        return result

    # ---- internals ----------------------------------------------------

    def _find_transport(self, gateway_id: Optional[str]):
        ident = str(gateway_id or "").upper()
        if not ident:
            return None
        try:
            transports = list(getattr(self.controller, "transports", None) or [])
        except Exception:
            # swallow-ok: legacy controller without the list-shim
            transports = []
        for t in transports:
            if str(getattr(t, "ident_mac", "") or "").upper() == ident:
                return t
        return None

    def _snapshot_gateway_rf(self, transport) -> Optional[dict]:
        try:
            res = self.gateway_service.query_gateway_rf_config(
                transport=transport,
            )
        except Exception:
            logger.exception(
                "ChannelScanService: query_gateway_rf_config raised",
            )
            return None
        if not isinstance(res, dict) or not res.get("ok"):
            return None
        cfg = res.get("rf_config")
        return dict(cfg) if isinstance(cfg, dict) else None

    @staticmethod
    def _emit_progress(progress_cb, **fields) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(dict(fields))
        except Exception:
            # swallow-ok: progress is best-effort; a misbehaving
            # callback never aborts the scan.
            logger.debug(
                "ChannelScanService progress_cb raised", exc_info=True,
            )
