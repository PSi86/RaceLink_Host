from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional, Union

from racelink.core import HostApi
from racelink.domain import (
    RL_Device,
    RL_DeviceGroup,
    RL_FLAG_HAS_BRI,
    RL_FLAG_POWER_ON,
    RL_Network,
    build_specials_state,
    create_device,
    state_scope,
)
from racelink.services import (
    ConfigService,
    ControlService,
    DiscoveryService,
    GatewayService,
    OnboardingService,
    StartblockService,
    StatusService,
    StreamService,
    SyncService,
)
from racelink.state import get_runtime_state_repository
from racelink.state.migrations import migrate_state
from racelink.state.persistence import (
    CURRENT_SCHEMA_VERSION,
    dump_records,
    dump_state,
    load_records,
    load_state,
    try_parse_legacy_repr,
)
from racelink.transport import (
    EthernetTransport,
    GatewaySerialTransport,
    LP,
    mac_last3_from_hex,
)

logger = logging.getLogger(__name__)


# Structured gateway-error codes surfaced in ``last_gateway_error.code``.
# WebUI consumers (and log aggregators) can route on the code instead of
# pattern-matching the free-form ``reason`` text.
GW_ERR_NOT_FOUND = "NOT_FOUND"   # no matching USB-serial gateway present
GW_ERR_PORT_BUSY = "PORT_BUSY"   # port exists but held by another process
GW_ERR_LINK_LOST = "LINK_LOST"   # transport disconnected after being ready
GW_ERR_HOST_ERROR = "HOST_ERROR"  # catch-all (unexpected local failure)

# Backoff schedule (seconds) for automatic gateway retries. The last entry
# is clamped, i.e. any attempt >= len(schedule) uses the final value.
#
# 2026-05-18 adjustment: shorten the early probes so a USB unplug+replug
# within the first few seconds is detected quickly (operator-perceived
# "I plugged it back in, why is the host still waiting?" gap). 
# 6×5 s mid-cadence → 10 s steady state forever. With
# attempt-index clamping the resulting cadence is 5,5,5,5,5,5,10,10,10,…
_GATEWAY_RETRY_BACKOFF_S = (5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 10.0)


def classify_gateway_error(reason: str, *, fallback: str = GW_ERR_HOST_ERROR) -> str:
    """Map a free-form gateway error message to a structured code.

    We prefer sniffing the message text over wrapping ``serial.SerialException``
    because the same strings are already raised by ``discover_and_open`` and
    surfaced through several code paths (``schedule_reconnect``, manual retry,
    startup). Returning ``fallback`` keeps unexpected errors visible without
    hiding them behind the retry machinery.
    """
    text = str(reason or "").lower()
    if not text:
        return fallback
    if "no racelink gateway" in text or "not found" in text or "no device" in text:
        return GW_ERR_NOT_FOUND
    if (
        "exclusive lock" in text
        or "could not exclusively lock" in text
        or "resource temporarily unavailable" in text
        or "port busy" in text
    ):
        return GW_ERR_PORT_BUSY
    if "disconnect" in text or "link lost" in text or "read error" in text:
        return GW_ERR_LINK_LOST
    return fallback


class RaceLink_Host:
    """Host controller coordinating runtime state, transport, and core services."""

    def __init__(
        self,
        host_api: "HostApi",
        name: str,
        label: str,
        state_repository=None,
    ):
        # The embedding host (RotorHazard plugin or standalone shim) must
        # satisfy the ``HostApi`` Protocol from ``racelink.core.host_api``.
        # The attribute is exposed as ``_host_api`` so plugin-specific names
        # do not leak into the Host codebase.
        self._host_api = host_api
        self.name = name
        self.label = label
        self.state_repository = state_repository or get_runtime_state_repository()
        # Stage 2: ``_transports`` is the primary list-backed store; the
        # ``self.transport`` property below preserves the single-transport
        # read/write contract every existing call site expects. Multi-
        # transport code paths (Stage 3+) iterate ``self.transports``.
        self._transports: list = []
        self.ready = False
        self.deviceCfgValid = False
        self.groupCfgValid = False

        # Transport-level pending expectation (for online/offline determination).
        # Mutated from two threads:
        #   * the TX-listener path (``GatewayService.on_transport_tx``)
        #     stamps a new expectation when an outbound unicast goes
        #     out — runs on whatever thread called ``_send_m2n``;
        #   * the RX-reader path (``pending_try_match`` /
        #     ``pending_window_closed``) reads the expectation and
        #     clears it on a matching reply or window-closed.
        # ``_pending_expect_lock`` keeps the read+clear atomic so a TX
        # thread cannot wedge a new expectation between an RX-thread
        # snapshot and its clear (lost-update). The clear helpers below
        # implement compare-and-clear semantics so a stale matcher
        # cannot wipe a freshly-stamped expectation either.
        #
        # Stage 2 Part 3: keyed by gateway_id (transport.ident_mac) so a
        # TX on transport-A does not overwrite a still-pending
        # expectation on transport-B. The legacy single-gateway path
        # stamps under key ``None`` and is unchanged at N=1.
        self._pending_expect: dict[Optional[str], dict] = {}
        self._pending_expect_lock = threading.Lock()

        # Stage 2 Part 3: track per-transport hook installation. The
        # earlier single ``bool`` would block re-installing on a second
        # attached transport. ``id(transport)`` is the identity key
        # because a freshly-built transport may not yet have an
        # ``ident_mac`` (set during discover_and_open).
        self._transport_hooks_installed_for: set[int] = set()
        # ``_pending_config`` is mutated from two threads:
        # the web request thread (``GatewayService.send_config`` stashes the
        # outgoing option/data0 keyed by recv3) and the RX reader thread
        # (``handle_ack_event`` pops the entry on a successful ACK). On
        # CPython a same-key write+pop race can lose the update silently;
        # any future iterator over the dict could also raise
        # ``RuntimeError: dictionary changed size during iteration``.
        # ``_pending_config_lock`` is held only across the dict mutation
        # itself — the long-running follow-up (``_apply_config_update``)
        # runs outside the lock so we never block the RX thread on it.
        self._pending_config: dict = {}
        self._pending_config_lock = threading.Lock()
        self._task_manager = None
        self._reconnect_in_progress = False
        self._last_reconnect_ts = 0.0
        self._last_error_notify_ts = 0.0
        # Plan P1-1: persistent gateway-failure state surfaced via /api/master
        # even when no user was driving the connection attempt.
        self.last_gateway_error: dict | None = None
        self._gateway_failure_count: int = 0
        # Auto-retry state. PORT_BUSY and LINK_LOST schedule an exp-backoff
        # retry. NOT_FOUND never auto-retries (hardware absent). The attempt
        # counter feeds the exponential delay and is reset on success or on
        # a manual retry.
        self._gateway_retry_timer: Optional[threading.Timer] = None
        self._gateway_retry_attempt: int = 0
        # Absolute wallclock (ms-since-epoch) when the pending retry timer
        # will fire. Used by gateway_status() to compute live
        # ``next_retry_in_s`` so the frontend banner countdown reflects the
        # actual remaining time, not a stale snapshot taken at error-record
        # time. ``None`` whenever no retry is pending.
        self._gateway_retry_fires_at_ms: Optional[int] = None
        # Startup-grace: the first discoverPort() runs before the user is even
        # able to click anything. Marking it as ``auto`` suppresses the RH
        # UI ERROR-alert path; subsequent auto-retries stay in the same mode.
        self._startup_done: bool = False
        # Link-recovery: once the gateway was ready at least once, treat any
        # subsequent ``NOT_FOUND`` as ``LINK_LOST`` so the auto-retry machinery
        # keeps polling until the dongle re-appears (USB unplug + replug).
        # Cleared on successful connect and on manual retry.
        self._link_recovery_pending: bool = False
        # Plan P2-2: plugins register a callback to refresh their panels after
        # state is persisted instead of monkey-patching load/save_to_db.
        self.on_persistence_changed = None
        # Plan P1-1: consumers (SSE layer, plugin UI) register a callback here
        # so a ready/last_error change produces a push notification rather
        # than requiring polling.
        self.on_gateway_status_changed = None
        # Plan P1-2: dispose transport cleanly when the host plugin unloads.
        self._shutdown_called: bool = False
        # WLED preset list (numeric ids -> labels). Pre-rename: ``uiEffectList``
        # — the entries are preset ids, not WLED effect-mode indices.
        # Basic colors: 1-9; Basic effects: 10-19; Special Effects (WLED only): 20-100
        self.uiPresetList = [
            {"value": "01", "label": "Red"},
            {"value": "02", "label": "Green"},
            {"value": "03", "label": "Blue"},
            {"value": "04", "label": "White"},
            {"value": "05", "label": "Yellow"},
            {"value": "06", "label": "Cyan"},
            {"value": "07", "label": "Magenta"},
            {"value": "10", "label": "Blink Multicolor"},
            {"value": "11", "label": "Pulse White"},
            {"value": "12", "label": "Colorloop"},
            {"value": "13", "label": "Blink RGB"},
            {"value": "20", "label": "WLED Chaser"},
            {"value": "21", "label": "WLED Chaser inverted"},
            {"value": "22", "label": "WLED Rainbow"},
        ]
        self.gateway_service = GatewayService(self)
        self.control_service = ControlService(self, self.gateway_service)
        self.config_service = ConfigService(self, self.gateway_service)
        self.discovery_service = DiscoveryService(self, self.gateway_service)
        self.status_service = StatusService(self, self.gateway_service)
        self.stream_service = StreamService(self, self.gateway_service)
        self.startblock_service = StartblockService(self, self.stream_service)
        self.sync_service = SyncService(self, self.gateway_service)
        self.onboarding_service = OnboardingService(self)
        # Stage 3 Part D: gateway bind-state machine. Wired up here so
        # ``_attach_transport`` can call ``evaluate`` during the boot/
        # reconnect flow; the SSE broadcaster is attached later by the
        # blueprint via ``gateway_bind_service.attach_broadcast``.
        from racelink.services.gateway_bind_service import GatewayBindService
        self.gateway_bind_service = GatewayBindService(
            controller=self,
            gateway_service=self.gateway_service,
            persist=lambda: self.save_to_db({}, scopes={state_scope.FULL}),
        )
        # Stage 3 Part E: multi-network RF migration engine. Bound back
        # to the bind service so the post-migration ``re_evaluate``
        # closes the conflict/pending loop on the SSE channel.
        from racelink.services.rf_migration_service import RfMigrationService
        self.rf_migration_service = RfMigrationService(
            controller=self,
            bind_service=self.gateway_bind_service,
        )
        # Stage 3 Part F: channel-scan service. Recovers devices that
        # got stranded on a previous migration by sweeping the
        # region's channel table on a chosen gateway and reporting
        # who's listening on each.
        from racelink.services.channel_scan_service import ChannelScanService
        self.channel_scan_service = ChannelScanService(controller=self)
        # Round 3: uniform per-network reconnect tracker. Polls
        # ``soft_rediscover`` while any expected (RL_Network.gateway_mac)
        # MAC is missing from the attached transports, surfacing the
        # missing set via the SSE ``gateway_missing`` event. Broadcast
        # is wired later by the blueprint.
        from racelink.services.missing_transport_tracker import MissingTransportTracker
        self.missing_transport_tracker = MissingTransportTracker(controller=self)

    def _option(self, key: str, default=None):
        return self._host_api.db.option(key, default)

    def _option_set(self, key: str, value) -> None:
        self._host_api.db.option_set(key, value)

    def _translate(self, text: str) -> str:
        return self._host_api.__(text)

    def _notify(self, message: str) -> None:
        ui = getattr(self._host_api, "ui", None)
        notify = getattr(ui, "message_notify", None) if ui else None
        if callable(notify):
            notify(message)

    def _broadcast_ui(self, panel: str) -> None:
        ui = getattr(self._host_api, "ui", None)
        broadcaster = getattr(ui, "broadcast_ui", None) if ui else None
        if callable(broadcaster):
            broadcaster(panel)

    def attach_task_manager(self, task_manager) -> None:
        self._task_manager = task_manager

    def is_discovery_active(self) -> bool:
        task_manager = getattr(self, "_task_manager", None)
        if task_manager is None:
            return False
        try:
            snap = task_manager.snapshot()
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            return False
        if not snap:
            return False
        return bool(snap.get("state") == "running" and snap.get("name") == "discover")

    @property
    def device_repository(self):
        return self.state_repository.devices

    @property
    def group_repository(self):
        return self.state_repository.groups

    @property
    def network_repository(self):
        return self.state_repository.networks

    # ---- Transport-list shim (Stage 2 part 2) ----------------------------
    #
    # The pre-Stage-2 code addresses a single attached gateway via
    # ``self.transport`` (read + write). To stay byte-identical for the
    # single-gateway deployment while internally supporting N transports,
    # ``transport`` is now a property that reads/writes the first slot of
    # ``self._transports``. Multi-transport code paths (Stage 3 + the
    # routing helpers below) use ``self.transports`` directly.
    @property
    def transport(self):
        return self._transports[0] if self._transports else None

    @transport.setter
    def transport(self, value):
        # ``self.transport = None`` clears the list (matches the legacy
        # disconnect / reconnect cycle's expectations). Any non-None
        # assignment replaces the single-slot contents — equivalent to
        # the old ``self.transport = GatewaySerialTransport(...)`` write.
        if value is None:
            self._transports = []
        else:
            self._transports = [value]

    @property
    def transports(self):
        """The live list of attached transports — usually 0 or 1 entries
        in Stage 2, up to N in Stage 3+. Mutate via ``transport`` setter
        for the single-slot case or via direct list ops for multi-slot."""
        return self._transports

    def transport_for_network(self, network_id):
        """Return the transport bound to the given ``RL_Network.id``.

        Resolution order:

        1. **Direct ``network_id`` binding** — every transport is stamped
           with its bound ``network_id`` at attach time
           (:meth:`_bind_transport_to_network`). Matching that stamp is the
           canonical, protocol-agnostic route: it works for transports that
           have *no* gateway MAC (e.g. an Ethernet transport, where the host
           NIC is the transport) and keeps the door open for several logical
           networks sharing one NIC later.
        2. **Legacy ``gateway_mac`` fallback** — for RF networks whose
           transport pre-dates the stamp or wasn't stamped, resolve via
           ``RL_Network.gateway_mac`` against the transport's ``ident_mac``.
        3. **Single-transport fallback** — a network with no MAC binding yet
           (single-gateway deployment) routes to the only transport.

        Returns ``None`` if no transport carries that network — either
        because the gateway is unplugged or because the network has never
        been bound to a physical unit.
        """
        if not network_id:
            return None
        target_nid = str(network_id)
        # (1) Direct network_id binding takes precedence.
        for t in self._transports:
            bound = getattr(t, "network_id", None)
            if bound and str(bound) == target_nid:
                return t
        net = self.network_repository.get_by_id(network_id)
        if net is None or not getattr(net, "gateway_mac", None):
            # (3) No MAC binding yet — single-gateway deployments don't
            # carry a per-network binding, so use the only transport.
            return self._transports[0] if len(self._transports) == 1 else None
        # (2) Legacy MAC-centric resolution.
        target_mac = str(net.gateway_mac).upper()
        for t in self._transports:
            ident = str(getattr(t, "ident_mac", "") or "").upper()
            if ident and ident == target_mac:
                return t
        return None

    def transport_for_device(self, addr):
        """Resolve the transport for a device by MAC.

        Looks up the device in the repository, reads its ``network_id``,
        and delegates to ``transport_for_network``. Single-gateway
        deployments hit the fallback in ``transport_for_network`` and
        return the only transport; the Stage 3 multi-network path
        routes via the explicit ``RL_Network.gateway_mac`` binding.
        """
        dev = self.getDeviceFromAddress(addr) if addr else None
        net_id = getattr(dev, "network_id", None) if dev is not None else None
        return self.transport_for_network(net_id)

    def transport_for_group(self, group_id):
        """Resolve the transport that owns the network this group lives
        on (Stage 3 Part G).

        Group sends (``OPC_PRESET`` / ``OPC_OFFSET`` / ``OPC_CONTROL``
        broadcast with ``recv3=FF:FF:FF``) need to land on the gateway
        whose radio actually carries the group's network. The
        boundary enforcement in Part B guarantees every group belongs
        to at most one network, so the resolution is unambiguous.

        Falls back to the single-transport slot when the group has
        no ``network_id`` (legacy / unmigrated payload) or no
        transport carries that network yet — matches the Stage-2
        N=1 behaviour.
        """
        try:
            gid_int = int(group_id) & 0xFF
        except (TypeError, ValueError):
            return self._transports[0] if len(self._transports) == 1 else None
        groups = list(self.group_repository.list())
        if 0 <= gid_int < len(groups):
            net_id = getattr(groups[gid_int], "network_id", None)
            routed = self.transport_for_network(net_id) if net_id else None
            if routed is not None:
                return routed
        return self._transports[0] if len(self._transports) == 1 else None

    @property
    def backup_device_repository(self):
        return self.state_repository.backup_devices

    @property
    def backup_group_repository(self):
        return self.state_repository.backup_groups

    def onStartup(self, _args) -> None:
        self.load_from_db()
        # First-ever gateway probe runs before the user can interact. Tag it
        # as ``auto`` so a bad outcome stays at WARNING and does not trip the
        # RotorHazard log-to-UI-alert bridge. Auto-retry machinery takes over
        # from there for PORT_BUSY / LINK_LOST.
        self.discoverPort({}, origin="auto")
        self._startup_done = True

    def save_to_db(self, args, scopes=None) -> None:
        """Persist devices + groups atomically under a single combined key.

        Writing both payloads together eliminates the partial-state hazard we
        used to have with separate ``rl_device_config`` / ``rl_groups_config``
        writes (see plan P1-5). The legacy keys are left untouched so an
        operator can roll back to an older Host build without losing data.

        ``scopes`` describes which user-visible state was mutated and is
        forwarded to ``on_persistence_changed`` so plugins can avoid rebuilding
        panels that are not affected. Callers that do not know the scope
        should omit the argument, which falls back to ``{FULL}`` for
        backwards-compatibility.
        """
        logger.debug("RL: Writing current states to Database (combined)")
        groups_to_dump = self.group_repository.list()
        if len(groups_to_dump) < len(self.backup_group_repository.list()):
            groups_to_dump = self.backup_group_repository.list()
        config_str_state = dump_state(
            self.device_repository.list(),
            groups_to_dump,
            self.network_repository.list(),
            schema_version=CURRENT_SCHEMA_VERSION,
        )
        self._option_set("rl_state_v1", config_str_state)
        self._fire_persistence_changed(scopes)

    def _fire_persistence_changed(self, scopes=None) -> None:
        """Invoke ``on_persistence_changed`` with a scope set, tolerating old signatures."""
        on_changed = getattr(self, "on_persistence_changed", None)
        if not callable(on_changed):
            return
        resolved = state_scope.normalize_scopes(scopes)
        try:
            on_changed(resolved)
        except TypeError:
            try:
                on_changed()
            except Exception:
                logger.exception("RaceLink: on_persistence_changed callback failed")
        except Exception:
            logger.exception("RaceLink: on_persistence_changed callback failed")

    def _load_from_legacy_keys(self):
        """Fall back to the pre-P1-5 per-key storage.

        Plan P1-3: if a legacy key contains pre-JSON Python-repr text (from
        very old Host builds that used ``ast.literal_eval``), attempt a one-
        shot migration via :func:`try_parse_legacy_repr`. The combined-key
        save triggered afterwards by ``load_from_db`` replaces both legacy
        keys, so this path runs at most once per deployment.
        """
        config_str_devices = self._option("rl_device_config", None)
        config_str_groups = self._option("rl_groups_config", None)
        if config_str_devices is None and config_str_groups is None:
            return None, None, True  # untouched; initialize from backups

        devices = self._load_legacy_records(
            config_str_devices,
            source="rl_device_config",
            backup=self.backup_device_repository.list(),
        )
        groups = self._load_legacy_records(
            config_str_groups,
            source="rl_groups_config",
            backup=self.backup_group_repository.list(),
        )
        return devices, groups, False

    def _load_legacy_records(self, raw, *, source: str, backup) -> list[dict]:
        """JSON first; if that warns, try the Python-repr migration once."""
        default = [obj.__dict__ for obj in backup]
        if raw in (None, ""):
            return default

        text = str(raw).strip()
        if text == "":
            return default
        # Cheap pre-check: JSON lists use double quotes; Python-repr uses single.
        looks_like_json = text.startswith("[{\"") or text.startswith("[{") and '"' in text[:40]
        if looks_like_json:
            return load_records(raw, default=default, source=source)

        salvaged = try_parse_legacy_repr(raw)
        if salvaged is not None:
            logger.warning(
                "RaceLink: migrated legacy Python-repr payload in %s (%d records); "
                "combined key will be written on next save.",
                source,
                len(salvaged),
            )
            return salvaged
        # Final fallback: let load_records log the warning and use the default.
        return load_records(raw, default=default, source=source)

    def load_from_db(self) -> None:
        logger.debug("RL: Applying config from Database")

        combined_raw = self._option("rl_state_v1", None)
        config_list_devices: list[dict]
        config_list_groups: list[dict]
        needs_migration_save = False

        config_list_networks: list[dict] = []
        if combined_raw in (None, ""):
            legacy_devices, legacy_groups, fresh_install = self._load_from_legacy_keys()
            if fresh_install:
                # No record at all -> initialize from backup defaults.
                config_list_devices = [obj.__dict__ for obj in self.backup_device_repository.list()]
                config_list_groups = [obj.__dict__ for obj in self.backup_group_repository.list()]
            else:
                config_list_devices = legacy_devices or []
                config_list_groups = legacy_groups or []
            needs_migration_save = True
            loaded_version = 0
        else:
            config_list_devices, config_list_groups, config_list_networks, loaded_version = load_state(
                combined_raw,
                default_devices=[obj.__dict__ for obj in self.backup_device_repository.list()],
                default_groups=[obj.__dict__ for obj in self.backup_group_repository.list()],
                default_networks=[],
                source="rl_state_v1",
            )
            if loaded_version == 0:
                # Combined key existed but was malformed; try legacy as a rescue.
                legacy_devices, legacy_groups, fresh_install = self._load_from_legacy_keys()
                if not fresh_install:
                    logger.warning(
                        "RaceLink: combined state unreadable; recovered from legacy keys"
                    )
                    config_list_devices = legacy_devices or []
                    config_list_groups = legacy_groups or []
                needs_migration_save = True

        config_list_devices, config_list_groups, config_list_networks, loaded_version = migrate_state(
            list(config_list_devices),
            list(config_list_groups),
            list(config_list_networks),
            from_version=loaded_version,
        )
        if loaded_version < CURRENT_SCHEMA_VERSION:
            needs_migration_save = True

        logger.debug(
            "RL: Loaded %d devices, %d groups, %d networks (schema_version=%s)",
            len(config_list_devices),
            len(config_list_groups),
            len(config_list_networks),
            loaded_version,
        )
        loaded_devices = []

        for device in config_list_devices:
            logger.debug(device)
            try:
                flags = device.get("flags", None)
                preset_id = device.get("presetId", None)

                if flags is None:
                    legacy_state = int(device.get("state", 1) or 0)
                    flags = RL_FLAG_POWER_ON if legacy_state else 0
                    if "brightness" in device:
                        flags |= RL_FLAG_HAS_BRI

                if preset_id is None:
                    preset_id = int(device.get("effect", 1) or 1)

                brightness = int(device.get("brightness", 70) or 0)

                dev_type = device.get("dev_type", None)
                if dev_type is None:
                    dev_type = device.get("device_type", None)
                if dev_type is None:
                    dev_type = device.get("caps", device.get("type", 0))

                # ``build_specials_state`` expects the canonical specials
                # sub-dict (keyed by flat option names). The persisted
                # device record carries that sub-dict under
                # ``device["specials"]``; passing the full record here
                # used to silently drop every persisted special back to
                # its schema default on every reload (iter-10 "Bug A").
                # The defensive unwrap added to ``build_specials_state``
                # in iter-10 makes both shapes work, but pass the
                # explicit sub-dict here so the call site documents
                # the actual contract.
                special_state = build_specials_state(
                    int(dev_type or 0),
                    device.get("specials") or {},
                )
                dev_obj = create_device(
                    addr=str(device.get("addr", "")).upper(),
                    dev_type=int(dev_type or 0),
                    name=str(device.get("name", "")),
                    groupId=int(device.get("groupId", 0) or 0),
                    version=int(device.get("version", 0) or 0),
                    caps=int(dev_type or 0),
                    flags=int(flags) & 0xFF,
                    presetId=int(preset_id) & 0xFF,
                    brightness=brightness & 0xFF,
                    specials=special_state,
                )
                # Multi-network fields (Stage 2). Backfilled by the
                # v1→v2 migration above; older payloads see ``None``
                # which is the by-design "unassigned" sentinel.
                net_id = device.get("network_id")
                if net_id:
                    dev_obj.network_id = str(net_id)
                lkrf = device.get("last_known_rf_config")
                if isinstance(lkrf, dict):
                    dev_obj.last_known_rf_config = dict(lkrf)
                loaded_devices.append(dev_obj)
            except Exception:
                logger.exception("RL: failed to load device entry from DB: %r", device)
                continue
        self.device_repository.replace_all(loaded_devices)

        if not config_list_groups:
            config_list_groups = [obj.__dict__ for obj in self.backup_group_repository.list()]

        loaded_groups = []
        for group in config_list_groups:
            logger.debug(group)
            group_dev_type = group.get("dev_type", group.get("device_type", 0))
            grp = RL_DeviceGroup(group["name"], group["static_group"], group_dev_type)
            net_id = group.get("network_id")
            if net_id:
                grp.network_id = str(net_id)
            loaded_groups.append(grp)

        loaded_groups = [
            group
            for group in loaded_groups
            if str(getattr(group, "name", "")).strip().lower() not in {"unconfigured", "all wled devices"}
        ]

        if not any(str(getattr(group, "name", "")).strip().lower() == "all wled nodes" for group in loaded_groups):
            loaded_groups.append(RL_DeviceGroup("All WLED Nodes", static_group=1, dev_type=0))
        else:
            for group in loaded_groups:
                if str(getattr(group, "name", "")).strip().lower() == "all wled nodes":
                    group.name = "All WLED Nodes"
                    group.static_group = 1
                    group.dev_type = 0
        self.group_repository.replace_all(loaded_groups)

        # Networks (Stage 2). The v1→v2 migration ensures at least the
        # default network is present; we hydrate every record into an
        # RL_Network object and replace_all so the repository's
        # identity comparisons (get_by_id / get_by_gateway_mac) work.
        loaded_networks = []
        for net in config_list_networks:
            if not isinstance(net, dict):
                continue
            try:
                loaded_networks.append(RL_Network(
                    id=net.get("id"),
                    name=str(net.get("name") or "Default"),
                    kind=net.get("kind"),
                    gateway_mac=net.get("gateway_mac"),
                    region=str(net.get("region") or "EU868"),
                    channel_id=net.get("channel_id"),
                    rf_config=net.get("rf_config"),
                    eth_config=net.get("eth_config"),
                    created_ts=net.get("created_ts"),
                ))
            except Exception:
                logger.exception("RL: failed to load network entry from DB: %r", net)
                continue
        self.network_repository.replace_all(loaded_networks)

        if needs_migration_save:
            try:
                self.save_to_db({}, scopes={state_scope.FULL})
            except Exception:
                logger.exception("RaceLink: failed to persist migrated state")
        else:
            # save_to_db fires this naturally; make sure it also fires for a
            # plain load so plugins can refresh panels (plan P2-2).
            self._fire_persistence_changed({state_scope.FULL})

    def _close_all_transports(self) -> None:
        """Release every attached transport and reset the slot list.

        Stage 2 Part 5: with multi-transport boot, every entry in
        ``self._transports`` holds an exclusive OS file-descriptor lock
        on its USB port. A fresh ``discoverPort`` walks
        :meth:`GatewaySerialTransport.enumerate_all` which probes every
        port — that probe would race the existing transports for the
        same locks, making each look ``PORT_BUSY``. Closing them all
        first preserves the pre-Part-5 cleanup invariant.
        """
        transports = list(self._transports)
        self._transports = []
        for t in transports:
            try:
                close = getattr(t, "close", None)
                if callable(close):
                    close()
            except Exception:
                logger.debug(
                    "RaceLink: error closing transport %r during reset",
                    getattr(t, "port", None), exc_info=True,
                )

    def _bind_transport_to_network(self, transport) -> Optional[str]:
        """Auto-bind ``transport`` to a persisted ``RL_Network`` (Stage 2 Part 5).

        Resolution order:

        1. If the transport carries an ``ident_mac`` and a network with
           the matching ``gateway_mac`` exists, bind to that network.
        2. If no exact match but the deployment is single-transport,
           bind to the first network without a ``gateway_mac`` and
           persist the MAC. Covers the v1→v2 default network's first
           contact: the migration leaves ``gateway_mac=None`` and this
           step fills it in.
        3. Otherwise leave the transport unbound and emit a WARNING.
           Stage 3 turns that into a ``gateway_unbound`` SSE event +
           operator wizard.

        Returns the bound ``network_id`` (or ``None`` if no binding was
        made). The binding is also stored on the transport as
        ``transport.network_id`` for the Stage-3 routing helpers.
        """
        ident = getattr(transport, "ident_mac", None) or None
        repo = self.network_repository
        bound_id: Optional[str] = None

        if ident:
            existing = repo.get_by_gateway_mac(ident)
            if existing is not None:
                bound_id = str(getattr(existing, "id", "") or "") or None

        if bound_id is None and ident:
            networks = list(repo.list())
            unbound = [n for n in networks if not getattr(n, "gateway_mac", None)]
            single_transport = len(self._transports) <= 1
            # Stage-2 policy: only auto-bind when we are confident the
            # operator has just one gateway. With multiple unknown
            # gateways we leave the rest unbound (Stage-3 wizard).
            if single_transport and unbound:
                target = unbound[0]
                target.gateway_mac = ident
                bound_id = str(getattr(target, "id", "") or "") or None
                try:
                    self.save_to_db({}, scopes={state_scope.FULL})
                except Exception:
                    logger.exception(
                        "RaceLink: failed to persist auto-bound gateway_mac=%s on network %s",
                        ident, bound_id,
                    )

        if bound_id is None and ident:
            # Stage-3 will turn this into a ``gateway_unbound`` SSE
            # event so the operator's wizard picks it up. Stage-2 just
            # logs — the transport stays attached but multi-network
            # routing helpers will return ``None`` for it.
            logger.warning(
                "RaceLink: gateway ident_mac=%s did not match any "
                "RL_Network (and auto-bind policy did not apply); "
                "transport stays unbound", ident,
            )

        try:
            setattr(transport, "network_id", bound_id)
        except Exception:
            # swallow-ok: read-only fake transports in tests are fine —
            # the canonical lookup is still via network.gateway_mac.
            logger.debug(
                "RaceLink: could not stamp network_id on transport %r",
                getattr(transport, "port", None), exc_info=True,
            )
        return bound_id

    def _fire_transport_rebind(self) -> None:
        """Notify subscribers (e.g. SSEBridge) that the transport set
        was just (re)bound. Pre-Part-5 this was inlined in
        ``discoverPort``; Part 5's multi-transport branches both need
        it so it moved out into a tiny helper.
        """
        on_rebind = getattr(self, "on_transport_rebind", None)
        if not callable(on_rebind):
            return
        try:
            on_rebind(self)
        except Exception:
            logger.debug("on_transport_rebind callback raised", exc_info=True)

    def _attach_transport(self, transport) -> Optional[str]:
        """Start ``transport``, append it to the slot list, install
        hooks, and run the network auto-bind. Returns the bound
        ``network_id`` or ``None``.
        """
        # Round 4 Task 2: idempotent attach. ``soft_rediscover`` (Round 3)
        # and the legacy ``discoverPort`` auto-retry path can race on the
        # same USB device — both opening ``/dev/ttyUSBx`` and calling
        # _attach_transport in quick succession. Without this guard we'd
        # end up with two transport objects carrying the same ident_mac,
        # one of them shadowed and its reader thread doomed to fire
        # EV_ERROR shortly afterwards. Detecting the duplicate here and
        # discarding the latecomer keeps ``_transports`` clean.
        ident = (getattr(transport, "ident_mac", None) or "").upper() or None
        if ident:
            for existing in list(self._transports):
                existing_ident = (getattr(existing, "ident_mac", None) or "").upper() or None
                if existing_ident == ident:
                    logger.warning(
                        "RaceLink: _attach_transport skipped duplicate for "
                        "ident_mac=%s (already attached on %s); closing the "
                        "redundant transport",
                        ident, getattr(existing, "port", "?"),
                    )
                    close = getattr(transport, "close", None)
                    if callable(close):
                        # Close async — the caller may be holding a lock
                        # the transport's read thread blocks on.
                        threading.Thread(
                            target=close, daemon=True,
                            name=f"rl-dup-transport-close-{ident}",
                        ).start()
                    return str(getattr(existing, "network_id", "") or "") or None

        try:
            transport.start()
        except Exception:
            logger.exception(
                "RaceLink: transport.start() failed on %s",
                getattr(transport, "port", None),
            )
            try:
                close = getattr(transport, "close", None)
                if callable(close):
                    close()
            except Exception:
                logger.debug(
                    "RaceLink: cleanup-close after failed start raised",
                    exc_info=True,
                )
            return None
        self._transports.append(transport)
        # Ethernet transports carry their ``network_id`` directly (the host NIC
        # is the transport — there is no gateway MAC to bind against). Skip the
        # MAC-based auto-bind, which would otherwise clear the pre-stamped
        # network_id, and the RF-only gateway bind-service evaluation below.
        is_ethernet = getattr(transport, "kind", "rf") == "ethernet"
        if is_ethernet:
            bound_id = getattr(transport, "network_id", None) or None
        else:
            bound_id = self._bind_transport_to_network(transport)
        # Install hooks per-transport (Part 3 made this per-id idempotent).
        self._install_transport_hooks(transport)
        if is_ethernet:
            # No RF bind state machine and no gateway state to probe for
            # Ethernet (constant IDLE) — attach is complete here.
            self._clear_gateway_error()
            return bound_id
        # Stage 3 Part D: run the bind state machine. The service
        # probes the gateway's NVS RF config and broadcasts the
        # ``gateway_bound`` / ``gateway_conflict`` / ``gateway_unbound``
        # SSE event so the WebUI can render the operator wizard. We
        # run inline here — the round-trip is ~500 ms USB-CDC and the
        # caller is already willing to wait for ``transport.start()``.
        # ``getattr`` keeps older tests that build the controller
        # without the bind service working unchanged.
        bind_service = getattr(self, "gateway_bind_service", None)
        if bind_service is not None:
            try:
                bind_service.evaluate(transport)
            except Exception:
                logger.exception(
                    "RaceLink: gateway_bind_service.evaluate raised for %s",
                    getattr(transport, "ident_mac", "?"),
                )
        # Round 5 follow-up: seed the per-gateway master state so the
        # MasterBar pill flips to its real colour right after attach
        # rather than sitting at grey/UNKNOWN until the operator hits
        # ↻ (or some spontaneous EV_STATE_CHANGED fires). The reply
        # comes back as EV_STATE_REPORT and the SSE bridge's listener
        # writes it into MasterStateMap; from there it fans out as a
        # ``master`` SSE event and the gateways store picks it up.
        # Fire-and-forget: the 1-byte send is non-blocking and we
        # don't need the synchronous return.
        try:
            send_state = getattr(transport, "send_state_request", None)
            if callable(send_state):
                send_state()
        except Exception:
            logger.debug(
                "RaceLink: post-attach send_state_request raised for %s",
                getattr(transport, "ident_mac", "?"), exc_info=True,
            )
        # Round 4 Task 1: cancel the global auto-retry timer that was
        # armed on the prior _record_gateway_error. Without this, a
        # successful soft_rediscover attach leaves the 5s timer ticking
        # — when it fires, ``discoverPort({}, origin="auto")`` calls
        # _close_all_transports() and tears down the freshly-attached
        # transport, restarting the disconnect/reconnect cycle.
        # ``_clear_gateway_error`` also resets failure counters + fires
        # the on_gateway_status_changed callback so the banner clears.
        self._clear_gateway_error()
        return bound_id

    # ---- Ethernet transport attach (Ethernet PoC) ------------------------

    @staticmethod
    def _eth_transport_kwargs(eth_config) -> dict:
        """Map a network's ``eth_config`` dict to ``EthernetTransport`` kwargs.

        Only known keys are forwarded so a forward-compatible config (extra
        fields) never trips the constructor. Missing keys fall back to the
        transport's own defaults.
        """
        cfg = eth_config if isinstance(eth_config, dict) else {}
        allowed = ("node_port", "host_port", "bind_host", "broadcast_host", "discovery")
        return {k: cfg[k] for k in allowed if k in cfg and cfg[k] is not None}

    def _attach_ethernet_transports(self) -> int:
        """Attach one ``EthernetTransport`` per persisted ``kind="ethernet"``
        network. Returns the number attached.

        Runs alongside (not instead of) the RF enumerate path: the host NIC is
        the transport, so there is no USB probe — each Ethernet network maps to
        exactly one UDP transport bound from its ``eth_config``. The duplicate
        guard in :meth:`_attach_transport` (keyed on the ``ETH:<id>`` ident)
        keeps a re-run from double-attaching.
        """
        count = 0
        try:
            networks = list(self.network_repository.list())
        except Exception:
            logger.debug("RaceLink: network_repository.list raised in _attach_ethernet_transports", exc_info=True)
            networks = []
        for net in networks:
            if getattr(net, "kind", "rf") != "ethernet":
                continue
            try:
                t = EthernetTransport(
                    network_id=str(getattr(net, "id", "") or ""),
                    on_event=None,
                    **self._eth_transport_kwargs(getattr(net, "eth_config", None)),
                )
                if not t.discover_and_open():
                    logger.warning(
                        "RaceLink: Ethernet transport for network %s failed to bind",
                        getattr(net, "id", "?"),
                    )
                    continue
                self._attach_transport(t)
                count += 1
                logger.info(
                    "RaceLink: Ethernet transport attached for network %s (%s) on %s",
                    getattr(net, "id", "?"), getattr(net, "name", "?"), t.port,
                )
            except Exception:
                logger.exception(
                    "RaceLink: failed to attach Ethernet transport for network %s",
                    getattr(net, "id", "?"),
                )
        return count

    def _handle_no_rf_gateway(self, *, reason: str, origin: str, code: str) -> None:
        """RF enumerate found nothing. If an Ethernet transport is attached the
        host is still ``ready`` (Ethernet-only deployment); otherwise record the
        RF gateway error as before.
        """
        has_ethernet = any(
            getattr(t, "kind", "rf") == "ethernet" for t in self._transports
        )
        if has_ethernet:
            self.ready = True
            self._link_recovery_pending = False
            self._clear_gateway_error()
            self._fire_transport_rebind()
            logger.info(
                "RaceLink: no RF gateway found, but %d Ethernet transport(s) "
                "attached — host ready via Ethernet",
                sum(1 for t in self._transports if getattr(t, "kind", "rf") == "ethernet"),
            )
            return
        self._record_gateway_error(reason=reason, origin=origin, code=code)
        if origin == "manual":
            self._notify(self._translate(reason))

    def format_gateway_label(self, gateway_id) -> str:
        """Compact human label for log prefixing: ``[#0 1C:10/Pit-Lane]``.

        Combines three pieces of context to make multi-gateway debug
        logs scannable without cross-referencing a separate boot line:

          * ``#N`` — index in ``_transports`` (attach order, useful for
            "is this the primary or secondary?").
          * ``XXXX`` — last 4 hex chars of ``ident_mac`` (stable per
            hardware unit, survives reconnects).
          * Network name from the bound ``RL_Network`` (operator-
            friendly identifier — Pit-Lane / Default / etc.).

        Returns ``[? unknown]`` when ``gateway_id`` is empty or no
        transport / network matches it (pre-handshake events,
        disconnected transports still emitting a final EV_ERROR).
        """
        if not gateway_id:
            return "[? unknown]"
        mac = str(gateway_id).upper()
        short = mac.replace(":", "")[-4:] or "????"
        idx: Optional[int] = None
        for i, t in enumerate(self._transports):
            if (getattr(t, "ident_mac", "") or "").upper() == mac:
                idx = i
                break
        network_name: Optional[str] = None
        try:
            net = self.network_repository.get_by_gateway_mac(mac)
            if net is not None:
                network_name = str(getattr(net, "name", "") or "") or None
        except Exception:
            # swallow-ok: label is diagnostic — fall back to MAC-only.
            network_name = None
        parts: list[str] = []
        if idx is not None:
            parts.append(f"#{idx}")
        parts.append(short)
        prefix = " ".join(parts)
        if network_name:
            return f"[{prefix}/{network_name}]"
        return f"[{prefix}]"

    def soft_rediscover(self) -> int:
        """Enumerate USB-attached RaceLink gateways and attach any that
        are NOT already present in ``self._transports``. Unlike
        :meth:`discoverPort`, this does NOT close existing transports —
        it is the per-network hot-reconnect path driven by
        :class:`MissingTransportTracker`.

        Returns the number of freshly-attached transports.
        """
        # Round 5 follow-up: pass the set of currently-attached ports
        # to ``enumerate_all`` so it does NOT probe them. Production
        # ``open()`` does not flock — probing an already-attached
        # /dev/ttyUSBx writes the IDENTIFY payload onto the live
        # gateway's USB-CDC stream and corrupts it, eventually firing
        # EV_ERROR on the previously-healthy transport. (Bench-test #6
        # cascade: B detach → tracker poll 5s later → enumerate_all
        # probes /dev/ttyUSB0 still owned by A → A's reader sees garbage
        # → A also detaches.)
        attached_ports = {
            getattr(t, "port", None)
            for t in self._transports
            if getattr(t, "port", None)
        }
        try:
            found = GatewaySerialTransport.enumerate_all(exclude_ports=attached_ports)
        except Exception:
            logger.exception("RaceLink: soft_rediscover enumerate_all raised")
            return 0

        attached_macs = {
            (getattr(t, "ident_mac", "") or "").upper()
            for t in self._transports
            if getattr(t, "ident_mac", None)
        }
        tracker = getattr(self, "missing_transport_tracker", None)
        cancelled = tracker.cancelled_macs() if tracker is not None else set()

        new_count = 0
        for port, ident_mac in found:
            mac_upper = (ident_mac or "").upper()
            if mac_upper and mac_upper in attached_macs:
                continue
            if mac_upper and mac_upper in cancelled:
                continue
            try:
                t = GatewaySerialTransport(port=port, on_event=None)
                if ident_mac:
                    try:
                        t.ident_mac = ident_mac
                    except Exception:
                        logger.debug(
                            "RaceLink: could not stamp ident_mac=%s on rediscovered transport %s",
                            ident_mac, port, exc_info=True,
                        )
                t.open()
            except Exception:
                logger.warning(
                    "RaceLink: soft_rediscover failed to open %s (ident_mac=%s); skipping",
                    port, ident_mac, exc_info=True,
                )
                continue
            bound_id = self._attach_transport(t)
            if bound_id is None and ident_mac is None:
                # _attach_transport already closed the transport on
                # start failure; nothing more to do.
                continue
            new_count += 1
            attached_macs.add(mac_upper)
            logger.info(
                "RaceLink: soft_rediscover attached %s (ident_mac=%s, network=%s)",
                port, ident_mac or "?", bound_id or "unbound",
            )
        return new_count

    @staticmethod
    def _normalize_comms_pins(value) -> list[str]:
        """Parse the ``rl_comms_port`` option into a list of pinned ports.

        Accepts a single port string (``"COM12"``), a comma-separated
        list (``"COM12,COM13"``), a native list, or a JSON-array string,
        so the same key works in the standalone JSON config and the
        RotorHazard DB (a plain string field). Empty / unset → ``[]``
        (auto-discovery).
        """
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            items = list(value)
        else:
            text = str(value).strip()
            if not text:
                return []
            if text.startswith("[") and text.endswith("]"):
                try:
                    parsed = json.loads(text)
                    items = parsed if isinstance(parsed, list) else [text]
                except (ValueError, TypeError):
                    items = text.split(",")
            else:
                items = text.split(",")
        return [str(x).strip() for x in items if str(x).strip()]

    def discoverPort(self, args, *, origin: Optional[str] = None) -> None:
        """Initialize the active gateway transports.

        ``origin`` describes who initiated the attempt and controls logging /
        UI notifications:
        - ``manual`` (default when ``args`` contains ``"manual"``): toast the
          result and escalate failures to ERROR.
        - ``auto``: scheduled from the background auto-retry timer or the very
          first startup probe -- silent, WARNING-level on failure.
        - ``programmatic``: any other caller (legacy).

        Pin behaviour via the ``rl_comms_port`` option:
        - **unset / empty** — auto-discovery: enumerate every
          USB-attached RaceLink gateway
          (:meth:`GatewaySerialTransport.enumerate_all`) and attach one
          transport per hit. At N=1 the end state matches the
          pre-Part-5 single-transport path.
        - **a single port** (``"COM12"``) — legacy single-port path:
          open exactly that device, skip the probe walk.
        - **multiple ports** (``"COM12,COM13"`` or a list) — multi-pin:
          enumerate, then attach only the gateways whose port is in the
          pin set, so a multi-gateway rig binds each transport to its
          network via the probed ``ident_mac``.

        Persistent failure state (``ready``, ``last_gateway_error``) is tracked
        in all cases so the UI can render its banner without relying on
        toasts.
        """
        if origin is None:
            origin = "manual" if "manual" in args else "programmatic"
        pinned_ports = self._normalize_comms_pins(self._option("rl_comms_port", None))

        # Release every previously-attached transport. Skipping this
        # step means the next ``enumerate_all`` probe (or the legacy
        # ``discover_and_open`` walk) fights the existing exclusive
        # locks and every port looks ``PORT_BUSY`` — see the
        # manual-retry-after-auto-recovery regression in Part 5 commit
        # history.
        self._close_all_transports()

        try:
            # Drop any stale per-transport hook flags so the upcoming
            # ``_install_transport_hooks`` actually re-installs against
            # the fresh transport instances.
            self._transport_hooks_installed_for.clear()

            # Ethernet transports attach independently of the RF enumerate
            # path (host NIC = transport, no USB probe). Run this first so an
            # Ethernet-only deployment is ``ready`` even when no RF gateway is
            # present (see ``_handle_no_rf_gateway``).
            self._attach_ethernet_transports()

            if len(pinned_ports) == 1:
                # Single manual pin — preserve the legacy single-port path.
                # ``discover_and_open`` with an explicit port skips the
                # walk and just opens the OS device; ident_mac stays
                # ``None`` unless the next IDENTIFY round-trip fills
                # it in.
                port_hint = pinned_ports[0]
                t = GatewaySerialTransport(port=port_hint, on_event=None)
                opened = False
                try:
                    opened = t.discover_and_open()
                except Exception:
                    raise
                if not opened:
                    if getattr(t, "last_discovery_had_busy_port", False):
                        reason = (
                            "RaceLink Gateway port busy: another process still holds "
                            "an exclusive lock. Retrying automatically."
                        )
                        self._record_gateway_error(
                            reason=reason, origin=origin, code=GW_ERR_PORT_BUSY,
                        )
                        if origin == "manual":
                            self._notify(self._translate(reason))
                        return
                    reason = "No RaceLink Gateway module discovered or configured"
                    self._handle_no_rf_gateway(
                        reason=reason, origin=origin, code=GW_ERR_NOT_FOUND,
                    )
                    return
                bound_id = self._attach_transport(t)
                self.ready = True
                self._link_recovery_pending = False
                self._clear_gateway_error()
                self._fire_transport_rebind()
                used = t.port or "unknown"
                mac = getattr(t, "ident_mac", None)
                if mac:
                    logger.info(
                        "RaceLink Gateway ready on %s with MAC: %s (network=%s)",
                        used, mac, bound_id or "unbound",
                    )
                    if origin == "manual":
                        self._notify(self._translate(
                            "RaceLink Gateway ready on {} with MAC: {}"
                        ).format(used, mac))
                return

            # No single pin — enumerate every USB-attached gateway. With a
            # multi-port pin set the list is then filtered to the pinned
            # ports; probing still yields each gateway's ``ident_mac`` so
            # multi-network binding works for the attached transports.
            try:
                gateways = GatewaySerialTransport.enumerate_all()
            except Exception:
                logger.exception("RaceLink: enumerate_all raised")
                gateways = []

            if pinned_ports:
                wanted = {p.lower() for p in pinned_ports}
                gateways = [(p, m) for (p, m) in gateways if str(p).lower() in wanted]
                found = {str(p).lower() for p, _ in gateways}
                missing = [p for p in pinned_ports if p.lower() not in found]
                if missing:
                    logger.warning(
                        "RaceLink: pinned comms port(s) not found or not "
                        "responding: %s", ", ".join(missing),
                    )

            if not gateways:
                reason = "No RaceLink Gateway module discovered or configured"
                self._handle_no_rf_gateway(
                    reason=reason, origin=origin, code=GW_ERR_NOT_FOUND,
                )
                return

            opened_count = 0
            primary_ident: Optional[str] = None
            primary_port: Optional[str] = None
            for port, ident_mac in gateways:
                try:
                    t = GatewaySerialTransport(port=port, on_event=None)
                    # ``enumerate_all`` already probed the gateway and
                    # extracted ident_mac. Stamp it before
                    # ``open()`` so the auto-bind step (and the SSE
                    # bridge's per-network routing) has it immediately.
                    if ident_mac:
                        try:
                            t.ident_mac = ident_mac
                        except Exception:
                            logger.debug(
                                "RaceLink: could not stamp ident_mac=%s on transport %s",
                                ident_mac, port, exc_info=True,
                            )
                    t.open()
                except Exception:
                    logger.warning(
                        "RaceLink: failed to open enumerated gateway %s "
                        "(ident_mac=%s); skipping",
                        port, ident_mac, exc_info=True,
                    )
                    continue
                bound_id = self._attach_transport(t)
                if bound_id is None and ident_mac is None:
                    # Could not even start the RX thread — _attach_transport
                    # closed the port. Move on.
                    continue
                if opened_count == 0:
                    primary_ident = ident_mac
                    primary_port = port
                opened_count += 1
                if origin == "manual":
                    self._notify(self._translate(
                        "RaceLink Gateway ready on {} with MAC: {}"
                    ).format(port, ident_mac or "?"))
                logger.info(
                    "RaceLink Gateway ready on %s with MAC: %s (network=%s)",
                    port, ident_mac or "?", bound_id or "unbound",
                )

            if opened_count == 0:
                reason = "No RaceLink Gateway module discovered or configured"
                self._handle_no_rf_gateway(
                    reason=reason, origin=origin, code=GW_ERR_NOT_FOUND,
                )
                return

            self.ready = True
            self._link_recovery_pending = False
            self._clear_gateway_error()
            self._fire_transport_rebind()
            if opened_count > 1:
                logger.info(
                    "RaceLink: %d gateways attached (primary=%s ident=%s)",
                    opened_count, primary_port or "?", primary_ident or "?",
                )
            # Round 3: re-evaluate the missing-transport tracker after
            # every successful discovery cycle so the gateway_missing
            # banner reflects the post-discovery state (and the poll
            # auto-arms if any RL_Network with a gateway_mac is still
            # missing from _transports).
            tracker = getattr(self, "missing_transport_tracker", None)
            if tracker is not None:
                try:
                    tracker.evaluate_and_arm()
                except Exception:
                    logger.exception(
                        "RaceLink: missing_transport_tracker.evaluate_and_arm raised",
                    )
        except Exception as ex:
            # swallow-ok: discoverPort surfaces failures via the
            # gateway-error record (red banner). Include the type so a
            # rare AttributeError from a renamed method is
            # distinguishable from the common SerialException path —
            # historically these all collapsed to ``str(ex)`` and were
            # indistinguishable in the operator-facing toast.
            logger.warning(
                "discoverPort failed: %s", type(ex).__name__, exc_info=True,
            )
            self._record_gateway_error(
                reason=f"{type(ex).__name__}: {ex}", origin=origin,
            )
            if origin == "manual":
                self._notify(self._translate("Failed to initialize communicator: {}").format(f"{type(ex).__name__}: {ex}"))

    def _record_gateway_error(self, *, reason: str, origin: str, code: Optional[str] = None) -> None:
        self.ready = False
        self._gateway_failure_count += 1
        resolved_code = code or classify_gateway_error(reason)

        # Once a connection has been established in this session, a follow-up
        # NOT_FOUND almost always means the user pulled the USB cable. Treat
        # it as LINK_LOST so the backoff timer keeps polling until the dongle
        # re-appears.
        if resolved_code == GW_ERR_NOT_FOUND and self._link_recovery_pending:
            resolved_code = GW_ERR_LINK_LOST

        # Decide whether to auto-retry. PORT_BUSY clears itself once the other
        # process releases the lock; LINK_LOST often clears once the dongle is
        # re-seated. NOT_FOUND does not, so we do not hammer the system for
        # absent hardware.
        auto_eligible = resolved_code in {GW_ERR_PORT_BUSY, GW_ERR_LINK_LOST}
        next_retry_in_s: Optional[float] = None
        if auto_eligible:
            idx = min(self._gateway_retry_attempt, len(_GATEWAY_RETRY_BACKOFF_S) - 1)
            next_retry_in_s = _GATEWAY_RETRY_BACKOFF_S[idx]

        # ``next_retry_in_s`` is intentionally NOT stored on the error dict
        # — gateway_status() recomputes it live from the active retry
        # timer's fire-time. Snapshotting it here would let the frontend
        # countdown drift past the actual schedule (e.g. when a transport
        # error retriggers _record_gateway_error after the timer already
        # advanced the backoff index).
        self.last_gateway_error = {
            "ts": time.time(),
            "reason": str(reason),
            "origin": origin,
            "code": resolved_code,
            "failure_count": int(self._gateway_failure_count),
        }

        # Only manual retries escalate to ERROR -- automatic / startup probes
        # that naturally fail should not spam the RotorHazard log-to-UI
        # bridge. A dongle that is merely unplugged at boot stays at WARNING.
        if origin == "manual":
            logger.error(
                "Gateway transport unavailable (origin=%s, code=%s, attempt=%s): %s",
                origin, resolved_code, self._gateway_failure_count, reason,
            )
        else:
            logger.warning(
                "Gateway transport unavailable (origin=%s, code=%s, attempt=%s): %s",
                origin, resolved_code, self._gateway_failure_count, reason,
            )

        if auto_eligible and next_retry_in_s is not None and not self._shutdown_called:
            self._schedule_gateway_retry(next_retry_in_s)

        self._notify_gateway_status()

    def _clear_gateway_error(self) -> None:
        was_unready = self.last_gateway_error is not None or not self.ready
        self.last_gateway_error = None
        self._gateway_failure_count = 0
        self._gateway_retry_attempt = 0
        self._cancel_gateway_retry()
        # Round 5 follow-up: clearing the error implies the gateway is
        # available. Pre-Round-4 only ``discoverPort`` called this and
        # had set ``self.ready = True`` itself one line earlier; the new
        # soft_rediscover → _attach_transport → _clear_gateway_error
        # path didn't, leaving ``controller.ready=False`` even though a
        # transport was successfully attached — visible as the legacy
        # "RaceLink Gateway is not available" banner persisting until
        # the operator hard-refreshed the browser.
        self.ready = True
        self._link_recovery_pending = False
        if was_unready:
            self._notify_gateway_status()

    def _schedule_gateway_retry(self, delay_s: float) -> None:
        """Arm a one-shot auto-retry of ``discoverPort`` after ``delay_s``.

        Only one timer is ever active. The retry increments
        ``_gateway_retry_attempt`` so the next scheduled delay progresses
        through the backoff schedule even if the current attempt fails
        quickly.
        """
        self._cancel_gateway_retry()
        attempt_next = self._gateway_retry_attempt + 1

        def _fire() -> None:
            if self._shutdown_called:
                return
            self._gateway_retry_attempt = attempt_next
            try:
                self.discoverPort({}, origin="auto")
            except Exception:
                logger.exception("RaceLink: auto-retry discoverPort raised")

        timer = threading.Timer(float(delay_s), _fire)
        timer.daemon = True
        timer.name = "rl-gateway-retry"  # A8: name daemon threads
        self._gateway_retry_timer = timer
        self._gateway_retry_fires_at_ms = int((time.time() + float(delay_s)) * 1000)
        timer.start()

    def _cancel_gateway_retry(self) -> None:
        timer = self._gateway_retry_timer
        self._gateway_retry_timer = None
        self._gateway_retry_fires_at_ms = None
        if timer is None:
            return
        try:
            timer.cancel()
        except Exception:
            logger.debug("RaceLink: error cancelling gateway retry timer", exc_info=True)

    def _notify_gateway_status(self) -> None:
        cb = getattr(self, "on_gateway_status_changed", None)
        if not callable(cb):
            return
        try:
            cb(self.gateway_status())
        except Exception:
            logger.exception("RaceLink: on_gateway_status_changed callback failed")

    def gateway_status(self) -> dict:
        """Return a JSON-serialisable gateway-readiness snapshot (plan P1-1).

        ``last_error.next_retry_in_s`` is computed live from the active
        retry timer's fire-time at each call, so an SSE ``gateway``
        broadcast (which serialises ``gateway_status()``) always carries
        the actual remaining countdown — never a stale snapshot.
        """
        last_error: Optional[dict] = None
        if self.last_gateway_error is not None:
            last_error = dict(self.last_gateway_error)
            fires_at = self._gateway_retry_fires_at_ms
            if fires_at is not None:
                remaining_ms = max(0, fires_at - int(time.time() * 1000))
                last_error["next_retry_in_s"] = round(remaining_ms / 1000.0, 3)
            else:
                last_error["next_retry_in_s"] = None
        return {
            "ready": bool(self.ready),
            "last_error": last_error,
            "failure_count": int(self._gateway_failure_count),
            "retry_attempt": int(self._gateway_retry_attempt),
        }

    def retry_gateway(self) -> dict:
        """User-driven retry; uses the manual-origin path so toasts still fire."""
        # Cancel any pending auto-retry and reset the exponential schedule --
        # the user just told us to try NOW, and the next failure should start
        # over at the shortest delay. Clearing ``_link_recovery_pending`` lets
        # the user escape a stuck LINK_LOST loop if they know the hardware is
        # truly gone and want to see the plain NOT_FOUND message again.
        self._cancel_gateway_retry()
        self._gateway_retry_attempt = 0
        self._link_recovery_pending = False
        self.discoverPort({"manual"}, origin="manual")
        return self.gateway_status()

    def shutdown(self) -> None:
        """Release the serial transport and flush persisted state (plan P1-2).

        Safe to call multiple times. Intended for plugin-unload / process-exit.
        """
        if self._shutdown_called:
            return
        self._shutdown_called = True
        self._cancel_gateway_retry()
        tracker = getattr(self, "missing_transport_tracker", None)
        if tracker is not None:
            try:
                tracker.shutdown()
            except Exception:
                logger.exception(
                    "RaceLink: missing_transport_tracker.shutdown raised",
                )
        # Stage 2 Part 5: close every attached transport, not just the
        # primary slot — at N>1 the secondary transports would
        # otherwise leak their exclusive OS file-descriptor locks past
        # shutdown.
        self._close_all_transports()
        task_manager = getattr(self, "_task_manager", None)
        if task_manager is not None:
            try:
                cancel = getattr(task_manager, "cancel", None)
                if callable(cancel):
                    cancel()
            except Exception:
                logger.exception("RaceLink: error cancelling task manager during shutdown")
        try:
            self.save_to_db({}, scopes={state_scope.NONE})
        except Exception:
            logger.exception("RaceLink: error persisting state during shutdown")
        # A7: release the auto-restore executor so its threads exit.
        # Best-effort — service may not have been fully wired in some
        # plugin-loader scenarios.
        gateway_svc = getattr(self, "gateway_service", None)
        if gateway_svc is not None:
            try:
                gateway_svc.shutdown()
            except Exception:
                logger.exception("RaceLink: error shutting down gateway service")
        self.ready = False

    def onRaceStart(self, _args) -> None:
        logger.warning("RaceLink Race Start Event")

    def onRaceFinish(self, _args) -> None:
        logger.warning("RaceLink Race Finish Event")

    def onRaceStop(self, _args) -> None:
        logger.warning("RaceLink Race Stop Event")

    def onSendMessage(self, args) -> None:
        logger.warning("Event onSendMessage")

    def getDevices(
        self,
        groupFilter: int = 255,
        targetDevice: Optional[RL_Device] = None,
        addToGroup: int = -1,
    ) -> int:
        result = self.discovery_service.discover_devices(
            group_filter=groupFilter,
            target_device=targetDevice,
            add_to_group=addToGroup,
        )
        found = int(result.get("found", 0) or 0)
        # Plan P2-8: `_notify` already handles the "no ui" case, so the local
        # hasattr guards are redundant -- drop them.
        if 0 < addToGroup < 255:
            msg = "Device Discovery finished with {} devices found and added to GroupId: {}".format(found, addToGroup)
        else:
            msg = "Device Discovery finished with {} devices found.".format(found)
        self._notify(msg)
        return found

    def getDevicesInGroups(
        self,
        groupIds,
        addToGroup: int = -1,
    ) -> int:
        """Sweep discovery over a list of group ids.

        Used by the WebUI's "Discover in: All groups" selector — see
        :meth:`DiscoveryService.discover_devices_in_groups`. Returns the
        aggregated reply count.
        """
        result = self.discovery_service.discover_devices_in_groups(
            group_ids=groupIds,
            add_to_group=addToGroup,
        )
        found = int(result.get("found", 0) or 0)
        if 0 < addToGroup < 255:
            msg = "Device Discovery sweep finished with {} replies; added to GroupId: {}".format(found, addToGroup)
        else:
            msg = "Device Discovery sweep finished with {} replies.".format(found)
        self._notify(msg)
        return found

    def getStatus(
        self,
        groupFilter: int = 255,
        targetDevice: Optional[RL_Device] = None,
    ) -> int:
        result = self.status_service.get_status(group_filter=groupFilter, target_device=targetDevice)
        return int(result.get("updated", 0) or 0)

    def setNodeGroupId(self, targetDevice: RL_Device, forceSet: bool = False, wait_for_ack: bool = True) -> bool:
        # Stage 2 Part 3: route the SET_GROUP through the transport that
        # owns this device's network. At N=1 ``transport_for_device``
        # falls back to the only attached transport — behaviour is
        # identical to the pre-multi-network path. With multiple
        # transports the SET_GROUP for an A-network device goes via
        # the A-network gateway, never via B.
        transport = self.transport_for_device(targetDevice.addr) or getattr(self, "transport", None)
        if transport is None:
            logger.warning("setNodeGroupId: communicator not ready")
            return False

        self._install_transport_hooks()

        recv3 = mac_last3_from_hex(targetDevice.addr)
        group_id = int(targetDevice.groupId) & 0xFF
        is_broadcast = recv3 == b"\xFF\xFF\xFF"

        if not is_broadcast:
            targetDevice.ack_clear()

        def _send():
            transport.send_set_group(recv3, group_id)

        if not wait_for_ack or is_broadcast:
            _send()
            return True

        events, _ = self.gateway_service.send_and_wait_with_retries(
            recv3, LP.OPC_SET_GROUP, _send, transport=transport,
        )
        if not events:
            logger.warning("No ACK_OK for SET_GROUP to %s (timeout)", targetDevice.addr)
            # The device didn't ACK within the timeout window — it's
            # not responding. Reflect that in the online flag so the
            # WebUI doesn't keep showing a non-responding device as
            # online. Mirrors the wording used by the auto-restore
            # path (gateway_service._spawn_auto_reassign_worker) and
            # the status-window timeout path (status_service).
            try:
                targetDevice.mark_offline("Missing reply (SET_GROUP)")
            except Exception:
                # swallow-ok: best-effort flag flip; the False return
                # below is the authoritative failure signal for the
                # caller. mark_offline only fails on malformed device
                # records which are already logged elsewhere.
                logger.debug(
                    "mark_offline failed after SET_GROUP timeout for %r",
                    getattr(targetDevice, "addr", "?"),
                    exc_info=True,
                )
            return False

        ev = events[-1]
        ok = int(ev.get("ack_status", 1)) == 0
        if not ok:
            logger.warning(
                "No ACK_OK for SET_GROUP to %s (status=%s, opcode=%s)",
                targetDevice.addr,
                ev.get("ack_status"),
                ev.get("ack_of"),
            )
        return ok

    def forceGroups(self, args=None, sanityCheck: bool = True) -> None:
        logger.debug("Forcing all known devices to their stored groups.")
        num_groups = len(self.group_repository.list())

        for device in self.device_repository.list():
            if sanityCheck is True and device.groupId >= num_groups:
                device.groupId = 0
            self.setNodeGroupId(device, forceSet=True)

    def _require_transport(self, context: str):
        if getattr(self, "transport", None):
            return True
        logger.warning("%s: communicator not ready", context)
        return False

    @staticmethod
    def _coerce_control_values(flags, preset_id, brightness, *, fallback: RL_Device | None = None):
        if fallback is not None:
            flags = fallback.flags if flags is None else flags
            preset_id = fallback.presetId if preset_id is None else preset_id
            brightness = fallback.brightness if brightness is None else brightness
        return int(flags) & 0xFF, int(preset_id) & 0xFF, int(brightness) & 0xFF

    def _update_group_control_cache(self, group_id: int, flags: int, preset_id: int, brightness: int) -> None:
        # A6: ``device_repository.list()`` returns the *live* storage.
        # Iterating it while another thread mutates the device list (a
        # gateway IDENTIFY can append; a delete can remove) used to risk
        # ``RuntimeError: list changed size during iteration``. The
        # ``state_repository.lock`` is a reentrant lock, so any caller
        # already holding it (e.g. the SSE refresh path) re-acquires
        # without deadlock.
        with self.state_repository.lock:
            for device in self.device_repository.list():
                try:
                    if (int(getattr(device, "groupId", 0)) & 0xFF) != group_id:
                        continue
                    device.flags = flags
                    device.presetId = preset_id
                    device.brightness = brightness
                except Exception:
                    # swallow-ok: bulk cache update keeps going on the
                    # remaining devices. Per-device failure here means
                    # malformed groupId / non-int field — a data
                    # quality issue worth diagnosing, so debug-log with
                    # traceback rather than silently dropping.
                    logger.debug(
                        "group-control cache update skipped device %r",
                        getattr(device, "addr", "?"),
                        exc_info=True,
                    )
                    continue

    def sendRaceLink(self, targetDevice, flags=None, presetId=None, brightness=None):
        """Compatibility entrypoint forwarding a fixed preset-id send to the
        control service (OPC_PRESET). Low-level shim kept for legacy callers."""
        return self.control_service.send_device_preset(targetDevice, flags, presetId, brightness)

    def sendGroupPreset(self, gcGroupId, gcFlags, gcPresetId, gcBrightness):
        """Broadcast a preset id to a group (OPC_PRESET)."""
        return self.control_service.send_group_preset(gcGroupId, gcFlags, gcPresetId, gcBrightness)

    def sendWledPreset(self, *, targetDevice=None, targetGroup=None, params=None):
        """Apply a classical WLED preset (OPC_PRESET)."""
        return self.control_service.send_wled_preset(
            targetDevice=targetDevice, targetGroup=targetGroup, params=params,
        )

    def sendWledResetOverrides(self, *, targetDevice=None, targetGroup=None, params=None) -> bool:
        """Clear all host-set RaceLink overrides on a WLED device (OPC_CONFIG 0x0F).

        Destructive: instructs the device to reset every
        ``RaceLink.overrides.*`` flag in its persisted ``cfg.json``.
        Policy A settings (FPS, ABL) revert to compile-time defaults
        on next boot; Policy B settings (segment geometry, briS,
        transition) revert to operator-saved cfg values. The host's
        stored ``dev.specials[wled_*]`` are also reset to schema
        defaults so the dialog rows no longer show a host-side
        override after the action.

        Unicast-only — OPC_CONFIG broadcasts are forbidden by design
        (different device classes interpret options differently).
        Group-target rejected. Non-WLED-capability devices rejected.

        Returns ``True`` on a successful ACK + state reset, ``False``
        otherwise. ``params`` is unused (the action carries no vars).
        """
        del params  # action has no vars
        if targetGroup is not None:
            return False
        if not targetDevice:
            return False

        try:
            from racelink.domain import get_dev_type_info  # type: ignore[no-redef]
        except ImportError:  # pragma: no cover - package-style fallback
            from .racelink.domain import get_dev_type_info  # type: ignore[no-redef]

        dev_type = int(getattr(targetDevice, "dev_type", getattr(targetDevice, "caps", 0)) or 0)
        caps = get_dev_type_info(dev_type).get("caps", []) or []
        if "WLED" not in caps:
            return False

        addr = str(getattr(targetDevice, "addr", "") or "")
        recv3 = mac_last3_from_hex(addr)
        if not recv3 or recv3 == b"\xFF\xFF\xFF":
            return False

        ok = self.sendConfig(
            option=0x0F,
            data0=0, data1=0, data2=0, data3=0,
            recv3=recv3,
            wait_for_ack=True,
            timeout_s=6.0,
        )
        if not ok:
            logger.warning(
                "RaceLink: sendWledResetOverrides ACK timeout for %s",
                addr,
            )
            return False

        # Reset host-side specials for the WLED options. Build a fresh
        # WLED-only defaults dict and mirror its keys onto dev.specials,
        # leaving non-WLED keys (e.g. STARTBLOCK slots/first_slot on a
        # combined device) untouched.
        try:
            wled_defaults = build_specials_state(dev_type, stored={})
            current = dict(getattr(targetDevice, "specials", {}) or {})
            for key, default in wled_defaults.items():
                if key.startswith("wled_"):
                    current[key] = int(default) & 0xFFFF
            targetDevice.specials = current
            try:
                self.save_to_db({"manual": True}, scopes={state_scope.DEVICE_SPECIALS})
            except Exception:
                # swallow-ok: in-memory reset already happened; SSE
                # refresh fired by the route still notifies the UI.
                logger.warning(
                    "save_to_db after sendWledResetOverrides failed",
                    exc_info=True,
                )
        except Exception:
            logger.exception(
                "RaceLink: sendWledResetOverrides reset of dev.specials raised for %s",
                addr,
            )
        logger.info(
            "RaceLink: sendWledResetOverrides OK for %s (host specials reset to defaults)",
            addr,
        )
        return True

    def sendRlPreset(self, *, targetDevice=None, targetGroup=None, params=None):
        """Apply a RaceLink-native preset (OPC_CONTROL) by its stable int id.

        This is the Specials/WebUI entry point for the ``rl_preset`` action
        (operator picks an RL preset id from the live preset list).
        ``params`` carries only ``{presetId, brightness}``; the host
        resolves the id via ``rl_presets_service`` before emitting
        OPC_CONTROL. Full 14-field parameter editing lives in the
        RL-preset editor (``dlgRlPresets``), not here. The raw
        direct-parameter sender stays available on ``ControlService``
        as :meth:`send_control` for internal callers.
        """
        params = params or {}
        preset_id = int(params.get("presetId", 0))
        brightness = params.get("brightness")
        return self.control_service.send_rl_preset_by_id(
            preset_id,
            targetDevice=targetDevice,
            targetGroup=targetGroup,
            brightness_override=int(brightness) if brightness is not None else None,
        )

    def sendRlPresetById(
        self,
        preset_id,
        *,
        targetDevice=None,
        targetGroup=None,
        brightness_override=None,
    ):
        """Apply a RL-preset snapshot (stable int id) via ControlService.

        RotorHazard quickset / default group action entry point. The service
        loads the persisted params through ``rl_presets_service`` and sends
        ``OPC_CONTROL``. WLED presets keep their own path via
        :meth:`sendWledPreset`.
        """
        return self.control_service.send_rl_preset_by_id(
            preset_id,
            targetDevice=targetDevice,
            targetGroup=targetGroup,
            brightness_override=brightness_override,
        )

    def sendStartblockConfig(self, *, targetDevice=None, targetGroup=None, params=None):
        """Compatibility entrypoint forwarding startblock config to StartblockService."""
        return self.startblock_service.send_startblock_config(
            target_device=targetDevice,
            target_group=targetGroup,
            params=params,
        )

    def runScene(self, scene_key, *, progress_cb=None):
        """Run a scene by key. Wired by ``RaceLinkApp`` factory; falls back to
        an explicit error result when the runner is not yet attached so the RH
        plugin's ``RaceLink Scene`` ActionEffect degrades gracefully on a
        partially-initialised controller.

        ``progress_cb`` (kwarg-only) forwards to the runner so the WebUI's
        synchronous ``/api/scenes/<key>/run`` route can broadcast SSE
        progress events. The RH plugin's ``applyScene`` path doesn't pass
        the kwarg, so its behaviour is unchanged.
        """
        runner = getattr(self, "scene_runner_service", None)
        if runner is None:
            from racelink.services.scene_runner_service import SceneRunResult
            return SceneRunResult(scene_key=str(scene_key), ok=False, error="runner_not_wired")
        return runner.run(str(scene_key), progress_cb=progress_cb)

    def _is_startblock_device(self, dev: RL_Device) -> bool:
        """Compatibility helper kept for legacy callers during controller slimming."""
        return self.startblock_service.is_startblock_device(dev)

    def _iter_startblock_devices(self, *, targetDevice=None, targetGroup=None) -> list[RL_Device]:
        """Compatibility helper kept for legacy callers during controller slimming."""
        return self.startblock_service.iter_startblock_devices(
            target_device=targetDevice,
            target_group=targetGroup,
        )

    def get_current_heat_slot_list(self):
        """Compatibility helper forwarding heat-slot lookup to the active source adapter."""
        return self.startblock_service.get_current_heat_slot_list()

    def sendStartblockControl(self, *, targetDevice=None, targetGroup=None, params=None):
        """Compatibility entrypoint forwarding startblock dispatch to StartblockService."""
        return self.startblock_service.send_startblock_control(
            target_device=targetDevice,
            target_group=targetGroup,
            params=params,
        )

    def _normalize_startblock_slot_list(self, slot_list):
        """Compatibility helper forwarding slot normalization to StartblockService."""
        return self.startblock_service.normalize_slot_list(slot_list)

    def _send_and_wait_for_reply(
        self,
        recv3: bytes,
        opcode7: int,
        send_fn,
        timeout_s: Optional[float] = None,
    ) -> tuple[list[dict], bool]:
        if timeout_s is None:
            return self.gateway_service.send_and_wait_for_reply(recv3, opcode7, send_fn)
        return self.gateway_service.send_and_wait_for_reply(recv3, opcode7, send_fn, timeout_s=timeout_s)

    def sendConfig(
        self,
        option,
        data0=0,
        data1=0,
        data2=0,
        data3=0,
        recv3=b"\xFF\xFF\xFF",
        wait_for_ack: bool = False,
        timeout_s: Optional[float] = None,
    ):
        """Compatibility entrypoint forwarding config writes to ConfigService.

        OPC_CONFIG must be unicast — see
        :meth:`ConfigService.send_config` for the design rule. Callers
        must pass a concrete ``recv3``; the broadcast default exists
        only as a defensive sentinel that the firmware drops.
        """
        return self.config_service.send_config(
            option,
            data0=data0,
            data1=data1,
            data2=data2,
            data3=data3,
            recv3=recv3,
            wait_for_ack=wait_for_ack,
            timeout_s=timeout_s,
        )

    def _apply_config_update(self, dev: RL_Device, option: int, data0: int) -> None:
        """Compatibility hook forwarding ACK-side config updates to ConfigService."""
        return self.config_service.apply_config_update(dev, option, data0)

    def stash_pending_config(self, recv3_hex: str, option: int, data0: int) -> None:
        """Record the option/data0 of an in-flight ``OPC_CONFIG`` keyed by
        the receiver's last-3 MAC bytes (uppercase hex).

        Called by ``GatewayService.send_config`` on the web/scene-runner
        side just before the transport write. The matching pop happens on
        the RX reader thread inside ``handle_ack_event`` once the gateway
        ACKs the config. The dedicated ``_pending_config_lock`` keeps the
        write+pop atomic without touching the broader state-repository
        lock, so a stalled RX handler cannot delay device-list mutations
        and vice versa.
        """
        with self._pending_config_lock:
            self._pending_config[recv3_hex] = {
                "option": int(option) & 0xFF,
                "data0": int(data0) & 0xFF,
            }

    def take_pending_config(self, recv3_hex: str) -> Optional[dict]:
        """Pop and return the recorded config payload for ``recv3_hex``.

        Returns ``None`` when no pending entry exists (e.g. broadcast
        ACK, duplicate ACK, or an entry that was already consumed).
        Held under the same lock as ``stash_pending_config``.
        """
        with self._pending_config_lock:
            return self._pending_config.pop(recv3_hex, None)

    def set_pending_expect(
        self,
        dev,
        rule,
        opcode7: int,
        sender_last3: str,
        ts: float,
        *,
        gateway_id: Optional[str] = None,
    ) -> None:
        """Stamp a pending unicast expectation. Called from the TX
        listener path right after a unicast request is on the wire.

        ``gateway_id`` is the originating transport's ``ident_mac``;
        ``None`` means the legacy single-gateway path. Stamping under a
        per-gateway key prevents a TX on transport-A from wiping an
        in-flight expectation on transport-B.
        """
        with self._pending_expect_lock:
            self._pending_expect[gateway_id] = {
                "dev": dev,
                "rule": rule,
                "opcode7": int(opcode7),
                "sender_last3": str(sender_last3 or "").upper(),
                "ts": float(ts),
                "gateway_id": gateway_id,
            }

    def read_pending_expect(self, gateway_id: Optional[str] = None) -> Optional[dict]:
        """Return the pending-expect dict for ``gateway_id`` (the live
        reference, not a copy). Callers must treat it as read-only and
        use :meth:`clear_pending_expect_if` for compare-and-clear
        semantics — clearing without the reference check would let a
        stale RX matcher wipe a freshly-stamped expectation from the
        TX thread.

        ``gateway_id=None`` returns the legacy single-gateway slot; a
        concrete value returns the per-transport slot. When no entry
        exists under the requested key but exactly one entry exists
        overall (single-transport runtime), the lone entry is returned
        — preserves the Stage-2 behaviour where untagged TX hooks /
        RX events still see the same expectation.
        """
        with self._pending_expect_lock:
            entry = self._pending_expect.get(gateway_id)
            if entry is not None:
                return entry
            if gateway_id is None and len(self._pending_expect) == 1:
                return next(iter(self._pending_expect.values()))
            return None

    def clear_pending_expect_if(self, expected: Optional[dict]) -> bool:
        """Atomic compare-and-clear: clear the matching slot only if it
        still holds the same dict reference as ``expected``. Returns
        True on a successful clear, False if the value has changed
        (i.e. a new TX-side stamp arrived in the meantime).

        This is the safe partner of :meth:`read_pending_expect` for the
        RX-thread "I matched the reply, drop the expectation" path —
        prevents the lost-update where the RX thread reads ``p``, the
        TX thread immediately stamps a new expectation, and the RX
        thread's clear wipes it. The expectation's own ``gateway_id``
        identifies which per-transport slot to drop.
        """
        if expected is None:
            return False
        gw_id = expected.get("gateway_id") if isinstance(expected, dict) else None
        with self._pending_expect_lock:
            current = self._pending_expect.get(gw_id)
            if current is expected:
                self._pending_expect.pop(gw_id, None)
                return True
            return False

    def clear_pending_expect(self, gateway_id: Optional[str] = None) -> None:
        """Unconditional clear. Used by paths that own the lifetime of
        the expectation (e.g. shutdown / reconnect) and are intentionally
        wiping any in-flight state. Most timeout/match callers should
        prefer :meth:`clear_pending_expect_if`.

        ``gateway_id=None`` wipes every slot (legacy and any per-
        transport entries); pass a concrete value to drop only one.
        """
        with self._pending_expect_lock:
            if gateway_id is None:
                self._pending_expect.clear()
            else:
                self._pending_expect.pop(gateway_id, None)

    def sendSync(self, ts24, brightness, recv3=b"\xFF\xFF\xFF", *, trigger_armed: bool = False):
        """Compatibility entrypoint forwarding sync packets to SyncService.

        ``trigger_armed`` defaults to ``False`` (clock-tick only); set it to
        ``True`` when this is a deliberate fire that should materialise
        pending arm-on-sync state. The scene runner's ``_run_sync`` already
        passes ``True`` through ``SyncService`` directly; this shim is only
        kept for any external compatibility callers.
        """
        return self.sync_service.send_sync(ts24, brightness, recv3=recv3,
                                           trigger_armed=trigger_armed)

    def sendStream(
        self,
        payload: bytes,
        groupId: int | None = None,
        device: RL_Device | None = None,
        retries: int = 2,
        timeout_s: float = 8.0,
    ) -> dict[str, int]:
        """Compatibility entrypoint forwarding payload streams to StreamService."""
        return self.stream_service.send_stream(payload, groupId=groupId, device=device, retries=retries, timeout_s=timeout_s)

    def _wait_rx_window(self, send_fn, collect_pred=None, fail_safe_s: float = 8.0):
        return self.gateway_service.wait_rx_window(send_fn, collect_pred=collect_pred, fail_safe_s=fail_safe_s)

    def _opcode_name(self, opcode7: int) -> str:
        return self.gateway_service.opcode_name(opcode7)

    def _log_transport_reply(self, ev: dict) -> None:
        return self.gateway_service.log_transport_reply(ev)

    def _log_rx_window_event(self, ev: dict) -> None:
        return self.gateway_service.log_rx_window_event(ev)

    def _handle_ack_event(self, ev: dict) -> None:
        return self.gateway_service.handle_ack_event(ev)

    def _install_transport_hooks(self, transport=None) -> None:
        # Pass the freshly-attached transport through so the service
        # installs its RX listener on THAT transport rather than
        # defaulting to ``self.transport`` (= _transports[0]). Without
        # this argument every _attach_transport call past the first
        # would re-install on the primary (already in installed_set
        # → early return) and the secondary transport's RX would
        # silently bypass on_transport_event, breaking IDENTIFY_REPLY
        # handling, link_online updates, and EV_ERROR disconnect
        # detection on every non-primary gateway.
        return self.gateway_service.install_transport_hooks(transport=transport)

    def _on_transport_tx(self, ev: dict) -> None:
        return self.gateway_service.on_transport_tx(ev)

    def _on_transport_event_gc(self, ev: dict) -> None:
        return self.gateway_service.on_transport_event(ev)

    def _schedule_reconnect(self, reason: str) -> None:
        return self.gateway_service.schedule_reconnect(reason)

    def _pending_try_match(self, ev: dict) -> None:
        return self.gateway_service.pending_try_match(ev)

    def _pending_window_closed(self, ev: dict) -> None:
        return self.gateway_service.pending_window_closed(ev)

    def getDeviceFromAddress(self, addr: str) -> Optional[RL_Device]:
        """MAC as a hex string without separators: 12 chars (full) or 6 chars (last 3 bytes)."""
        if not addr:
            return None
        s = str(addr).strip().upper()
        if len(s) == 12:
            return self.device_repository.get_by_addr(s)
        if len(s) == 6:
            return self.device_repository.get_by_addr(s)
        return None

    @staticmethod
    def _to_hex_str(addr: Union[str, bytes, bytearray, None]) -> str:
        if addr is None:
            return ""
        if isinstance(addr, (bytes, bytearray)):
            return bytes(addr).hex().upper()
        return str(addr).strip().replace(":", "").replace(" ", "").upper()
