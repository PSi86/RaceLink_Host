"""Guarded simulation / demo mode for the standalone Host.

Inert unless the ``RACELINK_SIM`` environment variable is set to a truthy
value. When enabled (typically by the screenshot capture harness in
``tools/screenshots/`` or for a hardware-free demo), :func:`apply_simulation`
seeds a deterministic demo dataset — devices, groups, networks, RL presets and
scenes with realistic English names — and attaches simulated transports so the
WebUI shows the **full** feature surface without any physical hardware:

* **2 RF gateways** backed by :class:`FakeRfTransport` (no serial port; each
  echoes its network's RF config on the bind probe so the gateway pill resolves
  to a green ``BOUND``/``IDLE`` state, exactly like a real attached gateway), and
* **1 Ethernet network** backed by the real :class:`EthernetTransport`, which
  already self-presents as a ready gateway with no hardware.

Nothing here runs on a normal launch — ``run_standalone`` only calls
:func:`apply_simulation` when :func:`is_sim_enabled` returns ``True``. The module
ships in the wheel but is dormant, so production behaviour is unchanged.
"""

from __future__ import annotations

import logging
import os

from ...domain import RL_Device, RL_DeviceGroup, RL_Network
from ...transport.gateway_events import (
    EV_RF_CHANGED,
    GATEWAY_STATE_IDLE,
    GATEWAY_STATE_NAME,
    RF_CHANGE_OK,
    RF_CHANGE_REASON_NAME,
)
from ...transport.gateway_serial import SendOutcome

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}

# Stable demo network identities so re-runs are deterministic (the screenshot
# hash-detection workflow wants byte-identical output for an unchanged UI).
NET_RF_MAIN = "11111111-1111-4111-8111-111111111111"
NET_RF_CHICANE = "22222222-2222-4222-8222-222222222222"
NET_ETH_PADDOCK = "33333333-3333-4333-8333-333333333333"

# Simulated gateway hardware MACs (uppercase 12-hex, like a real gateway ident).
GW_MAC_NORTH = "AA11BB22CC01"
GW_MAC_SOUTH = "AA11BB22CC02"

# Wire-format P_RfConfig dicts. The exact field values are cosmetic for the
# screenshots — what matters is that each network's persisted ``rf_config``
# matches what its FakeRfTransport echoes, so the bind diff is empty -> BOUND.
RF_CONFIG_MAIN = {
    "freq_hz": 868100000, "bw_khz_x10": 1250, "sf": 9, "cr_den": 5,
    "sync_word": 18, "tx_power_dbm": 14, "preamble": 8,
}
RF_CONFIG_CHICANE = {
    "freq_hz": 869525000, "bw_khz_x10": 1250, "sf": 10, "cr_den": 5,
    "sync_word": 18, "tx_power_dbm": 17, "preamble": 8,
}


def is_sim_enabled() -> bool:
    """True when ``RACELINK_SIM`` is set to a truthy value."""
    return os.environ.get("RACELINK_SIM", "").strip().lower() in _TRUE


class FakeRfTransport:
    """A hardware-free stand-in for :class:`GatewaySerialTransport`.

    Quacks like an RF transport (``kind="rf"``, a gateway ``ident_mac``, the
    listener / send-method surface) but performs no serial I/O. It reports a
    constant ``IDLE`` gateway state — picked up by the SSE bridge's
    ``_seed_master_state_from_transport`` to render a green master pill — and
    answers the bind service's ``GET_RF_CONFIG`` probe synchronously with its
    configured RF config so the bind record resolves to ``BOUND``.
    """

    kind = "rf"

    def __init__(self, ident_mac: str, rf_config: dict):
        self.ident_mac = str(ident_mac).upper()
        self.network_id = None  # stamped by the controller's auto-bind
        self.port = f"SIM:{self.ident_mac}"
        self.on_event = None
        self._rf_config = dict(rf_config)
        self._listeners: list = []
        self._tx_listeners: list = []
        self.last_discovery_had_busy_port = False

    # ---- gateway state shims (constant IDLE, like EthernetTransport) ------

    @property
    def gateway_state_byte(self) -> int:
        return int(GATEWAY_STATE_IDLE)

    @property
    def gateway_state_name(self) -> str:
        return GATEWAY_STATE_NAME.get(int(GATEWAY_STATE_IDLE), "IDLE")

    @property
    def gateway_state_metadata_ms(self) -> int:
        return 0

    def gateway_state_snapshot(self) -> dict:
        return {"state_byte": self.gateway_state_byte, "state": self.gateway_state_name,
                "state_metadata_ms": 0}

    # ---- lifecycle (no real I/O) -----------------------------------------

    def discover_and_open(self) -> bool:
        return True

    def open(self) -> None:
        return None

    def start(self) -> None:
        return None

    def close(self) -> None:
        return None

    # ---- listeners --------------------------------------------------------

    def add_listener(self, cb):
        if cb and cb not in self._listeners:
            self._listeners.append(cb)

    def remove_listener(self, cb):
        if cb in self._listeners:
            self._listeners.remove(cb)

    def add_tx_listener(self, cb):
        if cb and cb not in self._tx_listeners:
            self._tx_listeners.append(cb)

    def remove_tx_listener(self, cb):
        if cb in self._tx_listeners:
            self._tx_listeners.remove(cb)

    def _emit(self, ev: dict):
        ev.setdefault("gateway_id", self.ident_mac)
        for cb in list(self._listeners):
            try:
                cb(ev)
            except Exception:
                logger.debug("FakeRfTransport listener raised", exc_info=True)
        if self.on_event and self.on_event not in self._listeners:
            try:
                self.on_event(ev)
            except Exception:
                logger.debug("FakeRfTransport on_event raised", exc_info=True)

    def drain_events(self, timeout_s: float = 0.0):
        return []

    # ---- bind probe + state request --------------------------------------

    def send_get_rf_config(self) -> bool:
        """Answer the bind service's GET_RF_CONFIG probe immediately.

        The probe registers its callback via ``add_listener`` *before* calling
        this, so emitting ``EV_RF_CHANGED`` synchronously satisfies it without
        the 1.5 s timeout path — the bind record resolves to ``BOUND``.
        """
        self._emit({
            "type": EV_RF_CHANGED,
            "rf_config": dict(self._rf_config),
            "reason": RF_CHANGE_OK,
            "reason_name": RF_CHANGE_REASON_NAME.get(RF_CHANGE_OK, "ok"),
        })
        return True

    def send_state_request(self) -> bool:
        return True

    # ---- send path: no-op successes (idle screenshots never dispatch) -----

    def _ok(self, *_args, **_kwargs) -> SendOutcome:
        return SendOutcome.success()

    send_get_devices = _ok
    send_get_status = _ok
    send_preset = _ok
    send_wled_preset = _ok
    send_set_group = _ok
    send_indicate = _ok
    send_control = _ok
    send_config = _ok
    send_get_config = _ok
    send_sync = _ok
    send_offset = _ok
    send_stream = _ok
    send_rf_config = _ok
    send_get_rf_config_to_node = _ok


# ---- demo dataset --------------------------------------------------------

def _networks() -> list:
    return [
        RL_Network(id=NET_RF_MAIN, name="Main Straight", kind="rf",
                   gateway_mac=GW_MAC_NORTH, region="EU868", channel_id=0,
                   rf_config=dict(RF_CONFIG_MAIN)),
        RL_Network(id=NET_RF_CHICANE, name="Chicane", kind="rf",
                   gateway_mac=GW_MAC_SOUTH, region="EU868", channel_id=3,
                   rf_config=dict(RF_CONFIG_CHICANE)),
        RL_Network(id=NET_ETH_PADDOCK, name="Paddock LAN", kind="ethernet",
                   eth_config={"node_port": 5078, "host_port": 5079,
                               "bind_host": "0.0.0.0",
                               "broadcast_host": "255.255.255.255",
                               "discovery": "broadcast"}),
    ]


def _groups() -> list:
    # Index 0 MUST be the static "All WLED Nodes" group (the cross-network
    # filter view). Real user groups follow, so device.groupId == repo index:
    #   1=Start Gate  2=Turn 1 Arch  3=Finish Line  4=Podium Lights
    return [
        RL_DeviceGroup("All WLED Nodes", static_group=1, dev_type=0),
        RL_DeviceGroup("Start Gate", static_group=0, dev_type=0, network_id=NET_RF_MAIN),
        RL_DeviceGroup("Turn 1 Arch", static_group=0, dev_type=0, network_id=NET_RF_MAIN),
        RL_DeviceGroup("Finish Line", static_group=0, dev_type=0, network_id=NET_RF_CHICANE),
        RL_DeviceGroup("Podium Lights", static_group=0, dev_type=0, network_id=NET_ETH_PADDOCK),
    ]


# dev_type codes (racelink.domain.device_types.RL_Dev_Type): 12 = WLED_Rev4,
# 13 = WLED_Rev5, 50 = WLED_Startblock_Rev3 (carries STARTBLOCK + WLED caps).
_WLED = 12
_WLED5 = 13
_STARTBLOCK = 50


# 2S LiPo packs: healthy ~7.9 V, low-warning threshold defaults to 6800 mV.
def _dev(addr, name, group_id, network_id, dev_type=_WLED, *, voltage_mV=7950,
         node_rssi=-72, node_snr=9, effectId=0, presetId=1, brightness=160,
         online=True) -> RL_Device:
    d = RL_Device(addr=addr, dev_type=dev_type, name=name, groupId=group_id,
                  version=14, caps=dev_type, voltage_mV=voltage_mV,
                  node_rssi=node_rssi, node_snr=node_snr, presetId=presetId,
                  brightness=brightness, effectId=effectId, network_id=network_id)
    if online:
        d.mark_online()
    else:
        d.mark_offline("no recent contact")
    return d


def _devices() -> list:
    return [
        # Start Gate (RF: Main Straight) — incl. one Startblock + one low battery
        _dev("A1B2C3000101", "Start Gate Left", 1, NET_RF_MAIN, effectId=5, presetId=1, brightness=200),
        _dev("A1B2C3000102", "Start Gate Right", 1, NET_RF_MAIN, effectId=5, presetId=1, brightness=200),
        _dev("A1B2C3000103", "Start Gate Center", 1, NET_RF_MAIN, effectId=2, presetId=2, brightness=180, voltage_mV=6480, node_rssi=-88, node_snr=5),
        _dev("A1B2C3000104", "Start Beam", 1, NET_RF_MAIN, dev_type=_STARTBLOCK, effectId=0, presetId=1, brightness=120),
        # Turn 1 Arch (RF: Main Straight)
        _dev("A1B2C3000201", "Arch Apex", 2, NET_RF_MAIN, effectId=11, presetId=3, brightness=220, node_rssi=-69),
        _dev("A1B2C3000202", "Arch Left Post", 2, NET_RF_MAIN, effectId=11, presetId=3, brightness=220),
        _dev("A1B2C3000203", "Arch Right Post", 2, NET_RF_MAIN, dev_type=_WLED5, effectId=11, presetId=3, brightness=220, online=False),
        # Finish Line (RF: Chicane)
        _dev("A1B2C3000301", "Finish Banner", 3, NET_RF_CHICANE, effectId=8, presetId=4, brightness=255, node_rssi=-76),
        _dev("A1B2C3000302", "Finish Pole A", 3, NET_RF_CHICANE, effectId=8, presetId=4, brightness=255),
        _dev("A1B2C3000303", "Finish Pole B", 3, NET_RF_CHICANE, dev_type=_WLED5, effectId=8, presetId=4, brightness=255),
        # Podium Lights (Ethernet: Paddock LAN)
        _dev("A1B2C3000401", "Podium Step 1", 4, NET_ETH_PADDOCK, effectId=3, presetId=2, brightness=200, node_rssi=0, node_snr=0),
        _dev("A1B2C3000402", "Podium Step 2", 4, NET_ETH_PADDOCK, effectId=3, presetId=2, brightness=200, node_rssi=0, node_snr=0),
        # Unconfigured (groupId 0, no network) — populates the Unconfigured sink
        _dev("A1B2C3000999", "New Node (unassigned)", 0, None, effectId=0, presetId=1, brightness=70),
    ]


def _seed_state(rl) -> None:
    rl.network_repository.replace_all(_networks())
    rl.group_repository.replace_all(_groups())
    rl.device_repository.replace_all(_devices())
    try:
        rl.save_to_db({})
    except Exception:
        logger.debug("sim: save_to_db after seed raised", exc_info=True)


def _seed_presets(rl) -> None:
    svc = getattr(rl, "rl_presets_service", None)
    if svc is None:
        return
    presets = [
        ("Red Alert", {"mode": 0, "palette": 0, "color1": [255, 0, 0], "brightness": 255}),
        ("Green Go", {"mode": 0, "palette": 0, "color1": [0, 255, 0], "brightness": 255}),
        ("Rainbow Sweep", {"mode": 9, "palette": 11, "speed": 180, "intensity": 200, "brightness": 220}),
        ("Chase Blue", {"mode": 28, "color1": [0, 80, 255], "speed": 150, "intensity": 160, "brightness": 200}),
        ("Police Strobe", {"mode": 25, "color1": [255, 0, 0], "color2": [0, 0, 255], "speed": 240, "brightness": 255}),
    ]
    for label, params in presets:
        try:
            svc.create(label=label, params=params)
        except Exception:
            logger.debug("sim: preset create failed for %s", label, exc_info=True)


def _seed_scenes(rl) -> None:
    svc = getattr(rl, "scenes_service", None)
    if svc is None:
        return
    # Effect/preset vars live under ``params``; only ``duration_ms`` (delay) is
    # a top-level action field. ``startblock`` carries ``fn_key`` in params.
    race_start_actions = [
        {"kind": "rl_effect", "target": {"kind": "broadcast"},
         "params": {"mode": 1, "speed": 120, "intensity": 200, "palette": 0,
                    "color1": [255, 80, 0], "brightness": 180},
         "flags_override": {"arm_on_sync": True}},
        {"kind": "rl_preset", "target": {"kind": "groups", "value": [1]},
         "params": {"presetId": 1, "brightness": 220}, "flags_override": {"arm_on_sync": True}},
        {"kind": "wled_preset", "target": {"kind": "groups", "value": [3]},
         "params": {"presetId": 1, "brightness": 200}},
        {"kind": "startblock", "target": {"kind": "groups", "value": [1]},
         "params": {"fn_key": 1}},
        {"kind": "sync"},
        {"kind": "delay", "duration_ms": 1500},
        {"kind": "offset_group", "target": {"kind": "groups", "value": [1, 2]},
         "offset": {"mode": "linear", "base_ms": 0, "step_ms": 200},
         "actions": [
             {"kind": "rl_preset", "target": {"kind": "broadcast"},
              "params": {"presetId": 3, "brightness": 220}},
         ]},
    ]
    scenes = [
        {"label": "Race Start Sequence", "actions": race_start_actions, "stop_on_error": True},
        {"label": "Victory Lap", "actions": [
            {"kind": "rl_preset", "target": {"kind": "broadcast"},
             "params": {"presetId": 3, "brightness": 255}},
            {"kind": "sync"},
        ]},
        {"label": "Caution Yellow", "actions": [
            {"kind": "rl_effect", "target": {"kind": "broadcast"},
             "params": {"mode": 0, "color1": [255, 200, 0], "brightness": 200}},
        ], "network_scope": {"mode": "explicit", "network_ids": [NET_RF_MAIN]}},
    ]
    for scene in scenes:
        try:
            svc.create(**scene)
        except Exception:
            logger.debug("sim: scene create failed for %s", scene.get("label"), exc_info=True)


def _attach_transports(rl) -> None:
    # Ethernet first (binds the host NIC UDP socket; self-presents as ready).
    try:
        rl._attach_ethernet_transports()
    except Exception:
        logger.debug("sim: ethernet attach raised", exc_info=True)
    # Two simulated RF gateways — each binds to its network by gateway_mac.
    for mac, cfg in ((GW_MAC_NORTH, RF_CONFIG_MAIN), (GW_MAC_SOUTH, RF_CONFIG_CHICANE)):
        try:
            rl._attach_transport(FakeRfTransport(mac, cfg))
        except Exception:
            logger.debug("sim: RF attach raised for %s", mac, exc_info=True)
    # Install SSE hooks + seed each network's master pill from the attached
    # transports (mirrors the discoverPort path's post-attach rebind).
    try:
        rl._fire_transport_rebind()
    except Exception:
        logger.debug("sim: transport rebind raised", exc_info=True)


def apply_simulation(rl) -> None:
    """Seed the demo dataset and attach simulated transports onto ``rl``.

    Safe to call only when :func:`is_sim_enabled` — ``run_standalone`` guards
    the call. Idempotent enough for a single launch; not meant to be re-run on a
    live instance.
    """
    logger.info("RaceLink: RACELINK_SIM active — seeding demo dataset + simulated transports")
    _seed_state(rl)
    _seed_presets(rl)
    _seed_scenes(rl)
    _attach_transports(rl)
