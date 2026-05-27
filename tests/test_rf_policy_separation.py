"""Frequency-separation validator tests (Stage 3 Part A).

Pins :func:`racelink.domain.rf_policy.validate_networks_separation`:

  * Same SyncWord + close frequency  → conflict reported.
  * Same SyncWord + ≥500 kHz spread  → no conflict.
  * Different SyncWords (any spread) → no conflict.
  * Networks without rf_config are skipped (don't crash, don't report).
  * Conflicts carry both endpoints + the gap so the HTTP 400 message
    can be operator-readable.
  * Empty / single-network inputs are fine and return no conflicts.
  * The shipped :data:`REGION_CHANNELS` table self-passes the
    validator (channel-built networks never collide).
"""

from __future__ import annotations

import unittest

from racelink.domain.models import RL_Network
from racelink.domain.rf_channels import (
    REGION_CHANNELS,
    channel_rf_config,
)
from racelink.domain.rf_policy import (
    MIN_SEPARATION_HZ,
    format_conflict,
    validate_networks_separation,
)


def _net(name: str, *, freq_hz, sync_word=0x12, **extras):
    """Build an :class:`RL_Network` carrying just enough rf_config
    for the validator. The remaining wire-format fields are filled
    with the build-flag defaults so the dict is structurally complete
    (the validator only reads freq + sync, but a structurally complete
    config keeps the test realistic)."""
    return RL_Network(
        name=name,
        rf_config={
            "freq_hz": int(freq_hz),
            "bw_khz_x10": 1250,
            "sf": 7,
            "cr_den": 5,
            "sync_word": int(sync_word),
            "tx_power_dbm": 14,
            "preamble": 8,
            **extras,
        },
    )


class ConflictDetectionTests(unittest.TestCase):

    def test_no_conflict_when_far_apart(self):
        a = _net("A", freq_hz=867_700_000)
        b = _net("B", freq_hz=869_700_000)  # 2 MHz apart
        self.assertEqual(validate_networks_separation([a, b]), [])

    def test_exact_threshold_is_not_a_conflict(self):
        # The rule uses ``>=`` so exactly MIN_SEPARATION_HZ is OK.
        a = _net("A", freq_hz=867_700_000)
        b = _net("B", freq_hz=867_700_000 + MIN_SEPARATION_HZ)
        self.assertEqual(validate_networks_separation([a, b]), [])

    def test_below_threshold_same_sync_is_a_conflict(self):
        a = _net("A", freq_hz=867_700_000)
        b = _net("B", freq_hz=867_700_000 + (MIN_SEPARATION_HZ - 100_000))
        conflicts = validate_networks_separation([a, b])
        self.assertEqual(len(conflicts), 1)
        c = conflicts[0]
        # Endpoints + gap + reason all surfaced.
        self.assertEqual({c["a"]["name"], c["b"]["name"]}, {"A", "B"})
        self.assertEqual(c["gap_hz"], MIN_SEPARATION_HZ - 100_000)
        self.assertEqual(c["min_separation_hz"], MIN_SEPARATION_HZ)
        self.assertEqual(c["reason"], "freq_too_close_same_sync")

    def test_different_sync_words_never_conflict(self):
        # Even at 0 Hz separation, different SyncWords short-circuit
        # the rule — the PHY discriminator already prevents
        # cross-network frames from being demodulated.
        a = _net("A", freq_hz=867_700_000, sync_word=0x12)
        b = _net("B", freq_hz=867_700_000, sync_word=0x34)
        self.assertEqual(validate_networks_separation([a, b]), [])

    def test_skips_networks_without_rf_config(self):
        a = _net("A", freq_hz=867_700_000)
        b = RL_Network(name="B", rf_config=None)
        # No crash; no conflict reported.
        self.assertEqual(validate_networks_separation([a, b]), [])

    def test_skips_networks_with_incomplete_rf_config(self):
        # Missing ``sync_word`` — validator can't reason about the
        # pair, skips silently.
        a = _net("A", freq_hz=867_700_000)
        b = RL_Network(name="B", rf_config={"freq_hz": 867_800_000})
        self.assertEqual(validate_networks_separation([a, b]), [])

    def test_empty_input_returns_no_conflicts(self):
        self.assertEqual(validate_networks_separation([]), [])

    def test_single_network_returns_no_conflicts(self):
        a = _net("A", freq_hz=867_700_000)
        self.assertEqual(validate_networks_separation([a]), [])

    def test_three_networks_pairwise_conflicts(self):
        # A and B collide; A and C collide; B and C are spread out.
        a = _net("A", freq_hz=867_700_000)
        b = _net("B", freq_hz=867_900_000)  # 200 kHz from A
        c = _net("C", freq_hz=867_800_000)  # 100 kHz from A, 100 kHz from B
        conflicts = validate_networks_separation([a, b, c])
        pairs = {tuple(sorted([c_["a"]["name"], c_["b"]["name"]])) for c_ in conflicts}
        self.assertEqual(pairs, {("A", "B"), ("A", "C"), ("B", "C")})

    def test_self_pairs_never_reported(self):
        a = _net("A", freq_hz=867_700_000)
        # Same network listed twice (degenerate caller) — still no
        # self-conflict because the loop skips (i, j) with j <= i.
        self.assertEqual(validate_networks_separation([a, a]), [])


class ShippedChannelTableTests(unittest.TestCase):
    """The shipped :data:`REGION_CHANNELS` table must be a *valid*
    set under the policy. If the operator picks every channel in a
    region, the resulting networks should not flag against each
    other — otherwise the channel table itself is broken."""

    def test_eu868_channels_have_no_internal_conflicts(self):
        nets = [
            _net(f"EU{ch['id']}", freq_hz=ch["freq_hz"],
                 sync_word=ch["sync_word"])
            for ch in REGION_CHANNELS["EU868"]
        ]
        self.assertEqual(validate_networks_separation(nets), [])

    def test_us915_channels_have_no_internal_conflicts(self):
        nets = [
            _net(f"US{ch['id']}", freq_hz=ch["freq_hz"],
                 sync_word=ch["sync_word"])
            for ch in REGION_CHANNELS["US915"]
        ]
        self.assertEqual(validate_networks_separation(nets), [])

    def test_channel_rf_config_pipes_into_policy_without_loss(self):
        # End-to-end check: the helper that hands an rf_config to a
        # transport produces a dict the validator can reason about.
        cfg = channel_rf_config("EU868", 1)
        net = RL_Network(name="EU1", rf_config=cfg)
        self.assertEqual(validate_networks_separation([net]), [])


class CustomThresholdTests(unittest.TestCase):
    """The threshold is plumbed through so test code can simulate a
    tighter or looser rule. Production callers always use the
    module default."""

    def test_tightening_threshold_flags_previously_safe_pair(self):
        a = _net("A", freq_hz=867_700_000)
        b = _net("B", freq_hz=867_700_000 + MIN_SEPARATION_HZ)
        # Default threshold: no conflict.
        self.assertEqual(validate_networks_separation([a, b]), [])
        # Tightened threshold: now a conflict.
        conflicts = validate_networks_separation([a, b],
                                                 min_separation_hz=MIN_SEPARATION_HZ + 1)
        self.assertEqual(len(conflicts), 1)


class FormatConflictTests(unittest.TestCase):

    def test_format_conflict_includes_names_and_gap(self):
        a = _net("Track A", freq_hz=867_700_000)
        b = _net("Track B", freq_hz=867_900_000)
        conflicts = validate_networks_separation([a, b])
        self.assertEqual(len(conflicts), 1)
        msg = format_conflict(conflicts[0])
        self.assertIn("Track A", msg)
        self.assertIn("Track B", msg)
        self.assertIn("0x12", msg)
        # Gap is rendered in kHz for operator readability.
        self.assertIn("200 kHz", msg)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
