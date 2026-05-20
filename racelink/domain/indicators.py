"""Python mirror of the wire-stable indicator IDs in ``racelink_indicators.h``.

Hand-authored quick-start mirror; promote to a generated module (via
``gen_racelink_proto_py.py``) when the catalog grows enough that manual
sync becomes risky. IDs are append-only — they are wire-stable contract
bytes carried in ``P_Indicate.type``. Renumbering or repurposing breaks
older firmware that hashes against the previous values.

Visual parameters (fxMode, speed, color1, …) live exclusively on the
WLED side; the host only needs the IDs to pick which catalog row to
trigger. Duration is per-frame on the wire (``P_Indicate.durationSec``),
not part of the catalog.
"""
from __future__ import annotations

from enum import IntEnum


class IndicatorType(IntEnum):
    PAIR_CONFIRMED = 0
    PROBE_REJECTED = 1
    HEADLESS_ENTER = 2
    HEADLESS_EXIT  = 3
    IDENTIFY       = 4


# Default duration used by the operator-triggered "where is this device?"
# (Locate) click in the host UI when no ``duration_sec`` is supplied to
# ``POST /api/devices/indicate``. Five seconds is short enough not to
# disrupt an ongoing race, long enough to spot the strobing device by eye.
DEFAULT_INDICATE_DURATION_SEC = 5
