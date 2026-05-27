"""Region / channel table tests (Stage 3 Part A).

Pins the channel-table invariants:

  1. Each region has 1..MAX_CHANNELS_PER_REGION entries.
  2. Channel ids are 1-based and unique within a region.
  3. Every same-SyncWord pair in a region is ≥500 kHz apart
     (so the policy validator's separation rule never fails on
     a channel-table-only setup).
  4. Frequencies fall in the 800-1000 MHz plausible band.
  5. The lookup helpers (``get_channel`` / ``channel_rf_config``)
     return defensive copies + strip operator-visible metadata.

The table is shipped as code constants — these tests catch
table-edit mistakes before they ship.
"""

from __future__ import annotations

import unittest

from racelink.domain import rf_channels
from racelink.domain.rf_channels import (
    MAX_CHANNELS_PER_REGION,
    REGION_CHANNELS,
    channel_rf_config,
    get_channel,
    list_channels,
    list_regions,
)


class TableInvariantsTests(unittest.TestCase):
    """Pin the contract :func:`rf_channels._validate_table` enforces at
    import time. The module would refuse to import on violation, but
    the explicit assertions here make the invariant discoverable in
    tests too."""

    def test_table_has_at_least_one_region(self):
        self.assertGreaterEqual(len(REGION_CHANNELS), 1)

    def test_every_region_within_size_cap(self):
        for region, channels in REGION_CHANNELS.items():
            self.assertGreaterEqual(len(channels), 1, region)
            self.assertLessEqual(len(channels), MAX_CHANNELS_PER_REGION, region)

    def test_channel_ids_are_unique_per_region(self):
        for region, channels in REGION_CHANNELS.items():
            ids = [int(ch["id"]) for ch in channels]
            self.assertEqual(len(ids), len(set(ids)), region)

    def test_channel_ids_are_one_based_dense(self):
        # The WebUI dropdown reads ids verbatim — operator-facing UI
        # picks "Channel 1..N" by indexing, so the table must be
        # 1-based and dense (no gaps).
        for region, channels in REGION_CHANNELS.items():
            ids = sorted(int(ch["id"]) for ch in channels)
            self.assertEqual(ids, list(range(1, len(channels) + 1)), region)

    def test_every_channel_carries_full_p_rfconfig_fields(self):
        required = {
            "freq_hz", "bw_khz_x10", "sf", "cr_den",
            "sync_word", "tx_power_dbm", "preamble",
        }
        for region, channels in REGION_CHANNELS.items():
            for ch in channels:
                missing = required - set(ch.keys())
                self.assertEqual(missing, set(), f"{region} ch{ch.get('id')}: {missing}")

    def test_frequencies_in_plausible_sub_ghz_range(self):
        for region, channels in REGION_CHANNELS.items():
            for ch in channels:
                self.assertGreaterEqual(int(ch["freq_hz"]), 800_000_000, region)
                self.assertLessEqual(int(ch["freq_hz"]), 1_000_000_000, region)

    def test_same_sync_word_pairs_at_least_500khz_apart(self):
        for region, channels in REGION_CHANNELS.items():
            for i, a in enumerate(channels):
                for b in channels[i + 1:]:
                    if int(a["sync_word"]) != int(b["sync_word"]):
                        continue
                    gap = abs(int(a["freq_hz"]) - int(b["freq_hz"]))
                    self.assertGreaterEqual(
                        gap, 500_000,
                        f"{region} ch{a['id']} ↔ ch{b['id']} "
                        f"share SyncWord but are only {gap} Hz apart",
                    )


class LookupHelperTests(unittest.TestCase):

    def test_list_regions_returns_sorted_keys(self):
        regions = list_regions()
        self.assertEqual(regions, sorted(regions))
        # And every reported region actually exists in the table.
        for r in regions:
            self.assertIn(r, REGION_CHANNELS)

    def test_list_channels_returns_copies(self):
        # Mutating the returned list / dicts must not affect the
        # module-level table — otherwise a buggy caller could ruin
        # subsequent lookups.
        snapshot = list_channels("EU868")
        self.assertTrue(snapshot)
        snapshot[0]["freq_hz"] = 1
        self.assertNotEqual(REGION_CHANNELS["EU868"][0]["freq_hz"], 1)

    def test_list_channels_unknown_region_returns_empty(self):
        self.assertEqual(list_channels("ZZ999"), [])

    def test_get_channel_returns_copy_and_full_descriptor(self):
        ch = get_channel("EU868", 1)
        self.assertIsNotNone(ch)
        assert ch is not None  # type narrow
        self.assertEqual(int(ch["id"]), 1)
        self.assertEqual(ch["name"], "Default")
        # Defensive copy: mutate, then re-read, must see the original.
        ch["freq_hz"] = 0
        again = get_channel("EU868", 1)
        assert again is not None  # type narrow
        self.assertNotEqual(again["freq_hz"], 0)

    def test_get_channel_unknown_id_returns_none(self):
        self.assertIsNone(get_channel("EU868", 99))
        self.assertIsNone(get_channel("ZZ999", 1))
        self.assertIsNone(get_channel("EU868", None))  # type: ignore[arg-type]

    def test_channel_rf_config_strips_operator_metadata(self):
        cfg = channel_rf_config("EU868", 1)
        self.assertIsNotNone(cfg)
        assert cfg is not None  # type narrow
        self.assertNotIn("id", cfg)
        self.assertNotIn("name", cfg)
        # And carries every wire-format field.
        self.assertEqual(set(cfg.keys()), {
            "freq_hz", "bw_khz_x10", "sf", "cr_den",
            "sync_word", "tx_power_dbm", "preamble",
        })

    def test_channel_rf_config_unknown_returns_none(self):
        self.assertIsNone(channel_rf_config("EU868", 99))
        self.assertIsNone(channel_rf_config("ZZ999", 1))


class ValidateTableTests(unittest.TestCase):
    """The module-level ``_validate_table`` is called at import time —
    re-running it explicitly catches a deferred reload regression."""

    def test_validate_table_passes_on_shipped_table(self):
        # Should not raise.
        rf_channels._validate_table()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
