"""Tests for ``racelink.domain.node_config`` (§8a).

Pin the structural shape of the operator-facing CONFIG catalogue so a
future schema-format drift surfaces here rather than as a UI regression.
"""

from __future__ import annotations

import unittest

from racelink.domain.node_config import (
    CONFIG_BITS,
    NODE_CONFIG_COMMANDS,
    known_bits,
    known_options,
    serialize_node_config_schema,
)


class NodeConfigCatalogueTests(unittest.TestCase):
    # ---- NODE_CONFIG_COMMANDS ----------------------------------------

    def test_commands_are_present_and_well_formed(self):
        self.assertEqual(len(NODE_CONFIG_COMMANDS), 8)
        for cmd in NODE_CONFIG_COMMANDS:
            self.assertIn("value", cmd)
            self.assertIn("option", cmd)
            self.assertIn("data0", cmd)
            self.assertIn("label", cmd)
            self.assertIsInstance(cmd["value"], str)
            self.assertIsInstance(cmd["option"], int)
            self.assertIsInstance(cmd["data0"], int)
            self.assertTrue(cmd["label"], cmd)

    def test_command_values_are_unique(self):
        values = [cmd["value"] for cmd in NODE_CONFIG_COMMANDS]
        self.assertEqual(len(values), len(set(values)))

    def test_destructive_commands_carry_a_message(self):
        destructive = [c for c in NODE_CONFIG_COMMANDS if "destructive" in c]
        self.assertEqual(len(destructive), 2, "expected forget-mac + reboot")
        for cmd in destructive:
            self.assertIn("message", cmd["destructive"])
            self.assertTrue(cmd["destructive"]["message"])

    def test_known_options_matches_firmware_validation_set(self):
        # The receiver firmware validates ``option`` against this set;
        # if the host catalogue grows past it, the new entry would be
        # rejected on the wire.
        self.assertEqual(set(known_options()), {0x01, 0x03, 0x04, 0x80, 0x81})

    # ---- CONFIG_BITS -------------------------------------------------

    def test_config_bits_cover_all_eight_positions(self):
        self.assertEqual(len(CONFIG_BITS), 8)
        self.assertEqual([b["bit"] for b in CONFIG_BITS], list(range(8)))
        for bit in CONFIG_BITS:
            self.assertTrue(bit["label"], bit)

    def test_known_bits_helper_returns_the_canonical_range(self):
        self.assertEqual(known_bits(), tuple(range(8)))

    # ---- serialize_node_config_schema --------------------------------

    def test_serialize_returns_independent_copy(self):
        schema = serialize_node_config_schema()
        self.assertEqual(set(schema), {"commands", "bits"})
        self.assertEqual(len(schema["commands"]), len(NODE_CONFIG_COMMANDS))
        self.assertEqual(len(schema["bits"]), len(CONFIG_BITS))

        # Mutating the serialized copy must not affect the source-of-truth.
        schema["commands"].clear()
        schema["bits"].clear()
        self.assertEqual(len(NODE_CONFIG_COMMANDS), 8)
        self.assertEqual(len(CONFIG_BITS), 8)


if __name__ == "__main__":
    unittest.main()
