"""Region / channel lookup tables (Stage 3 Part A).

Operator-visible "channels" are a thin host-side indirection over
:data:`racelink.protocol.packets.RF_CONFIG_FIELDS` (the wire-format
``P_RfConfig`` dict). Firmware never sees the channel id — it always
receives the raw RF settings. The host translates "Network X uses
channel 2" into the seven-field dict the wire format expects.

Why a fixed table (rather than free-form per-network config):

  * Legal frequency selection is a compliance topic. Shipping the
    table as code constants keeps the operator from accidentally
    landing on a frequency outside their region's licensed band.
  * Stage-3 conflict detection needs a *named* anchor so the
    operator can read "Channel 2 (Alt-1)" instead of "867.1 MHz,
    SF7, BW125, SyncWord 0x12". The id is the stable handle; the
    name is for UI affordance only.
  * The frequency-separation validator (:mod:`racelink.domain.rf_policy`)
    only has to worry about networks the operator created from
    channels in this table — the table itself is pre-validated at
    import time (see :data:`_MIN_SEPARATION_HZ` and the assertions
    below).

Custom RF configs (operator types raw values in an "Advanced" form)
remain possible via the ``RL_Network.rf_config`` override — those
bypass the channel table entirely. The validator still runs on the
result so an Advanced-Mode entry that collides with a channel-based
network is flagged.

The table is intentionally short: at most 5 channels per region
(the Stage 3 plan's hard cap). Adding more would invite "operator
accidentally picks two near-neighbours" foot-guns; the migration
engine and channel scan are designed around a small, enumerable
set.
"""

from __future__ import annotations

from typing import Optional

# Minimum frequency separation between any two channels in the same
# region. Mirrors the validator's default in
# :mod:`racelink.domain.rf_policy`; pairs that share a SyncWord must
# be at least this far apart.
_MIN_SEPARATION_HZ = 500_000

# Default SyncWord — the build-flag value baked into the gateway and
# WLED firmware (RACELINK_SYNC_WORD=0x12). Channels with the same
# SyncWord must satisfy the separation rule; channels that *change*
# the SyncWord are immune (per the validator's "≥500 kHz OR
# different SyncWord" disjunction).
_DEFAULT_SYNC_WORD = 0x12

# Per-region channel descriptor list. Each entry mirrors the wire-
# format ``P_RfConfig`` fields plus the operator-visible ``id`` /
# ``name`` indirection.
REGION_CHANNELS: dict[str, list[dict]] = {
    # EU868: ISM band split across several sub-bands with different
    # duty-cycle ceilings. We pick five widely-separated 125-kHz
    # 7-spreading-factor anchors that span the most-used sub-bands
    # so the operator has visible spread without requiring SyncWord
    # juggling for collision avoidance.
    "EU868": [
        {
            "id": 1, "name": "Default",
            "freq_hz": 867_700_000, "bw_khz_x10": 1250, "sf": 7,
            "cr_den": 5, "sync_word": _DEFAULT_SYNC_WORD,
            "tx_power_dbm": 14, "preamble": 8,
        },
        {
            "id": 2, "name": "Alt-1",
            "freq_hz": 868_200_000, "bw_khz_x10": 1250, "sf": 7,
            "cr_den": 5, "sync_word": _DEFAULT_SYNC_WORD,
            "tx_power_dbm": 14, "preamble": 8,
        },
        {
            "id": 3, "name": "Alt-2",
            "freq_hz": 868_700_000, "bw_khz_x10": 1250, "sf": 7,
            "cr_den": 5, "sync_word": _DEFAULT_SYNC_WORD,
            "tx_power_dbm": 14, "preamble": 8,
        },
        {
            "id": 4, "name": "Alt-3",
            "freq_hz": 869_200_000, "bw_khz_x10": 1250, "sf": 7,
            "cr_den": 5, "sync_word": _DEFAULT_SYNC_WORD,
            "tx_power_dbm": 14, "preamble": 8,
        },
        {
            "id": 5, "name": "High",
            "freq_hz": 869_700_000, "bw_khz_x10": 1250, "sf": 7,
            "cr_den": 5, "sync_word": _DEFAULT_SYNC_WORD,
            "tx_power_dbm": 14, "preamble": 8,
        },
    ],
    # US915: 902-928 MHz ISM band. The five channels below sit well
    # inside the 902.3-914.9 MHz uplink range of the LoRaWAN US915
    # default plan, again with ≥500 kHz spread per pair.
    "US915": [
        {
            "id": 1, "name": "Default",
            "freq_hz": 904_300_000, "bw_khz_x10": 1250, "sf": 7,
            "cr_den": 5, "sync_word": _DEFAULT_SYNC_WORD,
            "tx_power_dbm": 14, "preamble": 8,
        },
        {
            "id": 2, "name": "Alt-1",
            "freq_hz": 905_300_000, "bw_khz_x10": 1250, "sf": 7,
            "cr_den": 5, "sync_word": _DEFAULT_SYNC_WORD,
            "tx_power_dbm": 14, "preamble": 8,
        },
        {
            "id": 3, "name": "Alt-2",
            "freq_hz": 906_300_000, "bw_khz_x10": 1250, "sf": 7,
            "cr_den": 5, "sync_word": _DEFAULT_SYNC_WORD,
            "tx_power_dbm": 14, "preamble": 8,
        },
        {
            "id": 4, "name": "Alt-3",
            "freq_hz": 907_300_000, "bw_khz_x10": 1250, "sf": 7,
            "cr_den": 5, "sync_word": _DEFAULT_SYNC_WORD,
            "tx_power_dbm": 14, "preamble": 8,
        },
        {
            "id": 5, "name": "High",
            "freq_hz": 908_300_000, "bw_khz_x10": 1250, "sf": 7,
            "cr_den": 5, "sync_word": _DEFAULT_SYNC_WORD,
            "tx_power_dbm": 14, "preamble": 8,
        },
    ],
}

# Hard cap from the Stage-3 plan. Adding a sixth channel to any
# region requires re-validating the separation invariant and
# updating the operator UI (which is built around the 1..5 id range).
MAX_CHANNELS_PER_REGION = 5


def list_regions() -> list[str]:
    """Stable, sorted list of region keys. Used by the WebUI's region
    dropdown — sorted so two hosts render the dropdown identically."""
    return sorted(REGION_CHANNELS.keys())


def list_channels(region: str) -> list[dict]:
    """Return a defensive copy of the channel list for ``region``.

    Returns ``[]`` for an unknown region rather than raising — the
    operator UI surfaces "no channels" instead of a 500 when the host
    is misconfigured.
    """
    raw = REGION_CHANNELS.get(str(region), [])
    return [dict(ch) for ch in raw]


def get_channel(region: str, channel_id: int) -> Optional[dict]:
    """Resolve ``(region, channel_id)`` to its descriptor dict.

    Returns ``None`` for an unknown region / id rather than raising —
    callers that build wire packets (e.g. the bind-service Stage-3
    auto-channel selection) handle the missing case with an
    operator-visible error.
    """
    if channel_id is None:
        return None
    for ch in REGION_CHANNELS.get(str(region), []):
        if int(ch.get("id", 0)) == int(channel_id):
            return dict(ch)
    return None


def channel_rf_config(region: str, channel_id: int) -> Optional[dict]:
    """Return only the wire-format ``P_RfConfig`` fields for the
    channel — strips the operator-visible ``id``/``name``. Used by
    the migration engine and bind service when handing an
    ``rf_config`` to a transport.
    """
    ch = get_channel(region, channel_id)
    if ch is None:
        return None
    return {
        "freq_hz": int(ch["freq_hz"]),
        "bw_khz_x10": int(ch["bw_khz_x10"]),
        "sf": int(ch["sf"]),
        "cr_den": int(ch["cr_den"]),
        "sync_word": int(ch["sync_word"]),
        "tx_power_dbm": int(ch["tx_power_dbm"]),
        "preamble": int(ch["preamble"]),
    }


# ---- Module-level self-check ------------------------------------------
#
# Catch table-edit mistakes at import time rather than runtime. Imports
# in production hit this once on first ``import racelink.domain``; tests
# exercise the same assertions explicitly via ``test_rf_channels.py``.

def _validate_table() -> None:
    """Internal: assert the shipped table satisfies the Stage-3 contract.

    * Each region has 1..``MAX_CHANNELS_PER_REGION`` entries.
    * Channel ids per region are unique and 1-based (UI assumption).
    * Every pair of channels with the same SyncWord is at least
      ``_MIN_SEPARATION_HZ`` apart in frequency — same rule the
      :mod:`racelink.domain.rf_policy` validator runs against
      operator-built networks.
    * Frequencies fit a sane "either EU868 sub-GHz or US915" range
      so a typo (e.g. 86 GHz instead of 868 MHz) never makes it past
      the import.
    """
    for region, channels in REGION_CHANNELS.items():
        if not (1 <= len(channels) <= MAX_CHANNELS_PER_REGION):
            raise AssertionError(
                f"region {region!r} has {len(channels)} channels "
                f"(must be 1..{MAX_CHANNELS_PER_REGION})"
            )
        ids = [int(ch["id"]) for ch in channels]
        if len(set(ids)) != len(ids):
            raise AssertionError(f"region {region!r} has duplicate channel ids: {ids}")
        for ch in channels:
            freq = int(ch["freq_hz"])
            # 800-1000 MHz covers EU868 + US915 with plenty of margin.
            if not (800_000_000 <= freq <= 1_000_000_000):
                raise AssertionError(
                    f"region {region!r} ch{ch['id']} freq {freq} Hz out of plausible band"
                )
        for i, a in enumerate(channels):
            for b in channels[i + 1:]:
                if int(a["sync_word"]) != int(b["sync_word"]):
                    continue
                gap = abs(int(a["freq_hz"]) - int(b["freq_hz"]))
                if gap < _MIN_SEPARATION_HZ:
                    raise AssertionError(
                        f"region {region!r} ch{a['id']} and ch{b['id']} "
                        f"share SyncWord 0x{a['sync_word']:02X} and are only "
                        f"{gap} Hz apart (need ≥{_MIN_SEPARATION_HZ})"
                    )


_validate_table()
