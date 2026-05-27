"""Host-side operator settings (battery thresholds, …).

Thin wrapper around the controller's option store (``_option`` /
``_option_set``) that keeps the read/write/normalize logic for
operator-facing host settings in one place. Today this is only the
battery low-voltage thresholds; future settings (warn-banner toggles,
display preferences, …) land here as additional sections.

The ``classify_voltage`` / ``is_low`` helpers are also exported so the
device DTO can flag weak batteries without re-deriving the rule.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Default thresholds chosen to match a typical RC-pack discharge curve:
# 2S Li-Po nominal 7.4 V, soft cut ~3.4 V/cell = 6.8 V → 6800 mV.
# 6S Li-Po nominal 22.2 V, soft cut ~3.4 V/cell = 20.4 V → 20400 mV.
BATTERY_DEFAULT_2S_MV = 6800
BATTERY_DEFAULT_6S_MV = 20400

# Hard validation ranges. Anything outside these bounds is rejected at
# the API boundary so the operator can't lock themselves into a
# threshold that classifies every device as weak.
BATTERY_RANGE_2S_MV = (4000, 8400)
BATTERY_RANGE_6S_MV = (12000, 25200)

# Class-detection split. Devices reporting below this voltage are
# treated as 2S, at-or-above as 6S. ``classify_voltage(0)`` returns
# ``"unknown"`` so a host that just rebooted (no STATUS_REPLY yet)
# doesn't flash false-positive warnings.
BATTERY_CLASS_SPLIT_MV = 12000


class HostSettingsService:
    """Operator-facing host settings backed by the option store."""

    def __init__(self, controller):
        self.controller = controller

    # -- battery thresholds ------------------------------------------------

    def get_battery_thresholds(self) -> dict:
        mv_2s = self._read_int("battery_threshold_2s_mV", BATTERY_DEFAULT_2S_MV)
        mv_6s = self._read_int("battery_threshold_6s_mV", BATTERY_DEFAULT_6S_MV)
        return {"mV_2s": mv_2s, "mV_6s": mv_6s}

    def set_battery_thresholds(self, mv_2s: int, mv_6s: int) -> dict:
        mv_2s_i = self._validate_range("2S", mv_2s, BATTERY_RANGE_2S_MV)
        mv_6s_i = self._validate_range("6S", mv_6s, BATTERY_RANGE_6S_MV)
        self.controller._option_set("battery_threshold_2s_mV", mv_2s_i)
        self.controller._option_set("battery_threshold_6s_mV", mv_6s_i)
        return {"mV_2s": mv_2s_i, "mV_6s": mv_6s_i}

    # -- classification helpers -------------------------------------------

    def classify_voltage(self, mV: Optional[int]) -> str:
        if mV is None:
            return "unknown"
        try:
            value = int(mV)
        except (TypeError, ValueError):
            return "unknown"
        if value <= 0:
            return "unknown"
        return "2s" if value < BATTERY_CLASS_SPLIT_MV else "6s"

    def is_low(self, mV: Optional[int]) -> bool:
        cls = self.classify_voltage(mV)
        if cls == "unknown":
            return False
        thresholds = self.get_battery_thresholds()
        try:
            value = int(mV)
        except (TypeError, ValueError):
            return False
        return value < int(thresholds["mV_2s" if cls == "2s" else "mV_6s"])

    # -- internals ---------------------------------------------------------

    def _read_int(self, key: str, default: int) -> int:
        raw = self.controller._option(key, default)
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "host_settings: option %r had non-integer value %r; falling back to %d",
                key, raw, default,
            )
            return default

    @staticmethod
    def _validate_range(label: str, value, allowed: tuple[int, int]) -> int:
        try:
            value_i = int(value)
        except (TypeError, ValueError) as ex:
            raise ValueError(f"{label} threshold must be an integer (got {value!r})") from ex
        low, high = allowed
        if not (low <= value_i <= high):
            raise ValueError(
                f"{label} threshold {value_i} mV outside allowed range "
                f"[{low}, {high}]"
            )
        return value_i
