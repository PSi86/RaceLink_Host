"""Regression: the shared RaceLink headers must stay byte-identical
across the repos that consume them — but the matrix is asymmetric.

Source of truth is the **WLED LoRa fork working tree**:
``../WLED LoRa/WLED/usermods/racelink_wled/`` (relative to this repo's
parent dir). The standalone ``RaceLink_WLED`` repo is a distribution
mirror that publishes the same content; ``RaceLink_Gateway/src``
carries copies because it builds a separate ESP32 firmware.

The Host repo carries only a **two-header subset** — the ones that
have an actual Python consumer:

* ``racelink_proto.h`` — parsed by ``gen_racelink_proto_py.py`` into
  ``racelink/racelink_proto_auto.py`` and referenced from many host
  modules (``protocol/packets.py``, ``domain/flags.py``,
  ``transport/gateway_events.py``, ...).
* ``racelink_indicators.h`` — guards the hand-authored Python mirror
  ``racelink/domain/indicators.py`` against drift on the wire-stable
  ``IND_*`` ids (append-only contract).

The FW-internal headers — ``racelink_headless.h``,
``racelink_transport_core.h`` and ``racelink_transport_common.h`` (the
medium-agnostic helpers + RX stream reassembly split out of core.h in
Block E) — live in the FW locations but have no Python consumer; the
Host repo does NOT carry them. The Ethernet backend headers
(``racelink_transport_eth.h``, ``racelink_w5500_udp.h``) are WLED-node
only — the LoRa Gateway does not build them, so they are checked between
the canonical and RaceLink_WLED but skipped for the Gateway sibling.

Hashes are compared with line endings normalized to LF, so this guard
flags real content drift rather than per-repo CRLF/LF differences.

Drift between any pair listed below is a real bug — opcodes / structs
/ flags shift on the wire in ways that look plausible end-to-end until
a specific combination of host build + firmware build deserialises
the same bytes differently.

Sibling locations may be absent (e.g. CI checks out only the Host).
When a sibling is missing, the comparison is skipped with a logged
message rather than failing.
"""

from __future__ import annotations

import hashlib
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# --- Header sets -------------------------------------------------------
#
# Host carries this subset (Pflicht — these MUST exist at Host top
# level). Adding a header here means a Python consumer relies on it.
HOST_HEADERS = (
    "racelink_proto.h",
    "racelink_indicators.h",
)

# FW headers every C++ build carries — the LoRa Gateway, the WLED node and the
# Ethernet node. Present in the canonical and BOTH FW siblings, never in Host.
# (racelink_transport_common.h was split out of racelink_transport_core.h in
# Block E Stage 1; core.h #includes it, so every transport build needs it.)
FW_SHARED_HEADERS = (
    "racelink_headless.h",
    "racelink_transport_core.h",
    "racelink_transport_common.h",
)

# FW headers only the WLED node tree carries (Ethernet backend, Block E). The
# LoRa Gateway build does not include them, so they are expected-absent (and
# silently skipped) for the Gateway sibling.
WLED_ONLY_HEADERS = (
    "racelink_transport_eth.h",
    "racelink_w5500_udp.h",
)

# FW-only = everything not in the Host subset (used by the "not in Host" guard).
FW_ONLY_HEADERS = FW_SHARED_HEADERS + WLED_ONLY_HEADERS

# Union — every header that lives anywhere in the matrix.
ALL_HEADERS = HOST_HEADERS + FW_ONLY_HEADERS


# --- Locations ---------------------------------------------------------
#
# Canonical lives in the WLED LoRa fork working tree. The standalone
# RaceLink_WLED repo is a publish mirror; RaceLink_Gateway/src is a
# build-time copy. All paths are resolved relative to ROOT.parent
# (= ``C:\Users\psima\Dev`` in the typical layout).
CANONICAL_DIR = ROOT.parent / "WLED LoRa" / "WLED" / "usermods" / "racelink_wled"
FW_SIBLINGS = {
    "WLED":    ROOT.parent / "RaceLink_WLED",
    "Gateway": ROOT.parent / "RaceLink_Gateway" / "src",
}


def _norm_hash(path: pathlib.Path) -> str:
    """SHA256 of the file content with line endings normalized to LF.

    The guard's purpose is to catch real protocol/transport drift, not EOL
    style: the sibling repos legitimately differ in CRLF/LF (e.g. the WLED-LoRa
    canonical stores some headers LF while RaceLink_WLED's working tree is CRLF
    via core.autocrlf). Normalizing newlines before hashing keeps the check
    robust across platforms while still flagging any byte-of-content change.
    """
    text = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(text).hexdigest()


class ProtoHeaderDriftTests(unittest.TestCase):

    def test_host_headers_exist(self):
        """The Host's own copies of the subset must always exist —
        ``racelink_proto.h`` is the source for ``racelink_proto_auto.py``
        and ``racelink_indicators.h`` guards the Python indicator mirror.
        The FW-only headers are not required at Host level."""
        for header in HOST_HEADERS:
            host_path = ROOT / header
            self.assertTrue(
                host_path.is_file(),
                f"Host {header} not found at {host_path}",
            )
        # Sanity: the FW-only headers should NOT linger in the Host
        # repo (dead-weight from before this subset rule was clarified).
        for header in FW_ONLY_HEADERS:
            stale = ROOT / header
            self.assertFalse(
                stale.is_file(),
                f"Host repo carries FW-only header {header} ({stale}) — "
                f"delete it; the Host has no Python consumer.",
            )

    def test_host_subset_matches_canonical(self):
        """Host's two-header subset must match the WLED-LoRa canonical
        byte-for-byte. Skipped if the canonical tree is absent (e.g. CI
        checkout with only the Host repo)."""
        if not CANONICAL_DIR.is_dir():
            self.skipTest(
                f"Canonical tree not present at {CANONICAL_DIR} — "
                f"skipping host-vs-canonical comparison.",
            )
        for header in HOST_HEADERS:
            host_hash = _norm_hash(ROOT / header)
            canon_hash = _norm_hash(CANONICAL_DIR / header)
            self.assertEqual(
                host_hash, canon_hash,
                f"{header} drift: Host vs Canonical sha256 differs.\n"
                f"  Host:      {host_hash}\n"
                f"  Canonical: {canon_hash}\n"
                f"Re-sync: copy {CANONICAL_DIR / header} -> {ROOT / header} "
                f"(see racelink_proto.h sync workflow in CLAUDE.md / "
                f"contributing.md).",
            )

    def test_fw_siblings_match_canonical(self):
        """Every FW sibling (RaceLink_WLED, RaceLink_Gateway/src) must
        carry the shared headers content-identical (EOL-normalized) to
        the canonical. WLED-only Ethernet headers are skipped for the
        Gateway sibling; any other sibling-missing file is a skipped
        comparison; canonical-missing a declared header is a hard fail."""
        if not CANONICAL_DIR.is_dir():
            self.skipTest(
                f"Canonical tree not present at {CANONICAL_DIR} — "
                f"skipping FW-sibling comparison.",
            )
        compared: list[str] = []
        skipped: list[str] = []
        for header in ALL_HEADERS:
            canon_path = CANONICAL_DIR / header
            if not canon_path.is_file():
                # Canonical missing a header that we expect is a hard
                # problem — fail rather than skip, because every other
                # location would silently match a phantom.
                self.fail(
                    f"Canonical missing {header} at {canon_path} — "
                    f"this header is declared in ALL_HEADERS but absent "
                    f"from the source of truth.",
                )
            canon_hash = _norm_hash(canon_path)
            for sibling_name, sibling_dir in FW_SIBLINGS.items():
                sibling_path = sibling_dir / header
                label = f"{sibling_name}/{header}"
                if not sibling_path.is_file():
                    skipped.append(label)
                    continue
                sibling_hash = _norm_hash(sibling_path)
                compared.append(label)
                self.assertEqual(
                    sibling_hash, canon_hash,
                    f"{header} drift: Canonical vs {sibling_name} differs.\n"
                    f"  Canonical:   {canon_hash}\n"
                    f"  {sibling_name + ':':12} {sibling_hash}\n"
                    f"Re-sync: copy {canon_path} -> {sibling_path}.",
                )

        # Smoke-print the set of (sibling, header) pairs actually
        # compared / skipped — useful in CI logs to confirm the test
        # exercised the intended targets.
        self.addCleanup(
            print,
            f"[proto-drift] compared: {compared if compared else '(none)'}; "
            f"skipped: {skipped if skipped else '(none)'}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
