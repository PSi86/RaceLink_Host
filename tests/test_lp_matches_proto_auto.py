"""Regression: every wire-protocol constant in ``racelink_proto_auto`` must
also be reachable as an attribute of ``LP`` (the transport-side alias used
by ``gateway_serial.py`` and friends).

Background — 2026-05-17 incident: ``LP`` was a hand-maintained class that
shadowed the auto-generated mirror with an explicit list of opcode
attributes and a ``getattr(RLPA, ...)`` override block. Adding a new
opcode to ``racelink_proto.h`` + regenerating ``racelink_proto_auto.py``
was not enough — the constant silently did **not** propagate to ``LP``
unless someone also added it to the class definition. ``OPC_INDICATE``
slipped through that gap and only surfaced at runtime as an
``AttributeError: type object 'LP' has no attribute 'OPC_INDICATE'``.

``LP`` is now a thin module-alias for ``racelink_proto_auto``; this test
pins that contract so a future refactor that re-introduces a class
shadow without mirroring the auto-symbols fails fast in CI.
"""

from __future__ import annotations

import unittest

from racelink import racelink_proto_auto as RLPA
from racelink.transport.gateway_events import LP

# Wire-protocol prefixes whose every constant in the auto-mirror should
# be reachable through ``LP``. Add a new prefix here when a new family
# of generator-emitted symbols lands (e.g. a future ``CFG_*`` group).
WIRE_PREFIXES = (
    "OPC_",
    "DIR_",
    "EV_",
    "ACK_",
    "GW_CMD_",
    "GW_STATE_",
    "TX_REJECT_",
    "OFFSET_MODE_",
    "SYNC_FLAG_",
)


class LPMatchesProtoAutoTests(unittest.TestCase):

    def test_lp_exposes_every_wire_constant(self) -> None:
        missing: list[str] = []
        mismatched: list[tuple[str, int, int]] = []
        for name in dir(RLPA):
            if not name.startswith(WIRE_PREFIXES):
                continue
            if not hasattr(LP, name):
                missing.append(name)
                continue
            auto_value = getattr(RLPA, name)
            lp_value = getattr(LP, name)
            if auto_value != lp_value:
                mismatched.append((name, auto_value, lp_value))

        if missing or mismatched:
            self.fail(
                "LP / racelink_proto_auto contract violation.\n"
                f"  Missing on LP: {missing}\n"
                f"  Mismatched (name, auto, LP): {mismatched}\n"
                "If you re-introduced a hand-maintained ``LP``, mirror EVERY "
                "auto-generated constant — or revert to the module-alias "
                "approach in gateway_events.py."
            )

    def test_lp_make_type_matches_auto(self) -> None:
        """``make_type`` is the only non-constant export used through LP;
        it must produce identical results from the auto-mirror function."""
        for direction in (RLPA.DIR_M2N, RLPA.DIR_N2M):
            for opcode in (RLPA.OPC_PRESET, RLPA.OPC_INDICATE, RLPA.OPC_ACK):
                self.assertEqual(
                    LP.make_type(direction, opcode),
                    RLPA.make_type(direction, opcode),
                    f"LP.make_type diverged for direction={direction:#x} opcode={opcode:#x}",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
