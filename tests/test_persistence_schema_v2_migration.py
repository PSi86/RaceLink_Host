"""Schema v1 -> v2 migration tests (Stage 2).

The v2 schema introduces the multi-network data model: a ``networks``
list at the top level of the persisted payload, plus a ``network_id``
field on every device and group record. The migration must be:

  * Lossless on v1 payloads — every device/group survives with the
    same fields, plus ``network_id`` backfilled from the synthesised
    default network.
  * Idempotent — running the migration on a v2 payload that already
    carries the default network does not duplicate it or change its
    id.
  * Stable-id — the default network always uses
    ``DEFAULT_NETWORK_ID`` so a payload migrated on host A and on
    host B end up with the same default-network identity.
"""

from __future__ import annotations

import json
import unittest

from racelink.state.migrations import DEFAULT_NETWORK_ID, migrate_state
from racelink.state.persistence import (
    CURRENT_SCHEMA_VERSION,
    dump_state,
    load_state,
)


class V1ToV2MigrationTests(unittest.TestCase):

    def test_v1_payload_synthesises_default_network(self):
        devices = [{"addr": "AABBCCDDEEFF", "groupId": 3}]
        groups = [{"name": "G1", "static_group": 0, "dev_type": 0}]
        # No networks in v1 payload.
        d, g, n, ver = migrate_state(devices, groups, [], from_version=1)

        self.assertEqual(ver, CURRENT_SCHEMA_VERSION)
        self.assertEqual(len(n), 1)
        self.assertEqual(n[0]["id"], DEFAULT_NETWORK_ID)
        self.assertEqual(n[0]["name"], "Default")

    def test_v1_payload_backfills_network_id_on_devices_and_groups(self):
        devices = [
            {"addr": "AABBCCDDEEFF", "groupId": 1},
            {"addr": "112233445566", "groupId": 2},
        ]
        groups = [{"name": "Group A", "static_group": 0, "dev_type": 0}]
        d, g, n, _ = migrate_state(devices, groups, [], from_version=1)

        # Every device picks up the default network's id.
        for record in d:
            self.assertEqual(record.get("network_id"), DEFAULT_NETWORK_ID)
        # Every group too.
        for record in g:
            self.assertEqual(record.get("network_id"), DEFAULT_NETWORK_ID)

    def test_v1_payload_preserves_existing_fields(self):
        devices = [
            {"addr": "AA", "groupId": 5, "name": "WLED A", "brightness": 200}
        ]
        groups = [{"name": "Special", "static_group": 1, "dev_type": 11}]
        d, g, _, _ = migrate_state(devices, groups, [], from_version=1)

        self.assertEqual(d[0]["addr"], "AA")
        self.assertEqual(d[0]["groupId"], 5)
        self.assertEqual(d[0]["name"], "WLED A")
        self.assertEqual(d[0]["brightness"], 200)
        self.assertEqual(g[0]["name"], "Special")
        self.assertEqual(g[0]["static_group"], 1)
        self.assertEqual(g[0]["dev_type"], 11)

    def test_idempotent_when_default_network_already_present(self):
        # A v2 payload that already contains the default network must
        # round-trip unchanged.
        devices = [{"addr": "AA", "network_id": DEFAULT_NETWORK_ID}]
        groups = [{"name": "G", "network_id": DEFAULT_NETWORK_ID}]
        networks = [{
            "id": DEFAULT_NETWORK_ID, "name": "Default",
            "region": "EU868", "channel_id": None, "rf_config": None,
            "gateway_mac": None, "created_ts": 0.0,
        }]
        d, g, n, ver = migrate_state(
            devices, groups, networks, from_version=CURRENT_SCHEMA_VERSION,
        )
        self.assertEqual(ver, CURRENT_SCHEMA_VERSION)
        self.assertEqual(len(n), 1)
        self.assertEqual(n[0]["id"], DEFAULT_NETWORK_ID)
        self.assertEqual(d[0]["network_id"], DEFAULT_NETWORK_ID)
        self.assertEqual(g[0]["network_id"], DEFAULT_NETWORK_ID)

    def test_does_not_overwrite_existing_network_id(self):
        # If an upgraded payload (e.g. operator manually mid-stage)
        # carries a non-default network_id on some records, the
        # migration leaves those intact and only backfills the
        # missing ones.
        custom_id = "11111111-2222-3333-4444-555555555555"
        devices = [
            {"addr": "AA", "network_id": custom_id},
            {"addr": "BB"},  # missing -> defaults
        ]
        d, _, _, _ = migrate_state(devices, [], [], from_version=1)
        self.assertEqual(d[0]["network_id"], custom_id)
        self.assertEqual(d[1]["network_id"], DEFAULT_NETWORK_ID)

    def test_load_then_migrate_v1_payload_via_dump_pretending_to_be_v1(self):
        # End-to-end: a payload that mimics what a v1 host would have
        # written (no networks key). Load_state reports schema_version=1
        # because the dict carries ``schema_version: 1`` explicitly.
        v1_blob = json.dumps({
            "schema_version": 1,
            "devices": [{"addr": "AA", "groupId": 1}],
            "groups": [{"name": "G", "static_group": 0, "dev_type": 0}],
            # No 'networks' key on purpose — that's what v1 looks like.
        })
        d_raw, g_raw, n_raw, v = load_state(v1_blob)
        self.assertEqual(v, 1)
        self.assertEqual(n_raw, [])

        d, g, n, applied = migrate_state(d_raw, g_raw, n_raw, from_version=v)
        self.assertEqual(applied, CURRENT_SCHEMA_VERSION)
        self.assertEqual(len(n), 1)
        self.assertEqual(d[0]["network_id"], DEFAULT_NETWORK_ID)
        self.assertEqual(g[0]["network_id"], DEFAULT_NETWORK_ID)

    def test_dump_then_load_roundtrip_v2_with_networks(self):
        devices = [{"addr": "AA", "network_id": DEFAULT_NETWORK_ID}]
        groups = [{"name": "G", "network_id": DEFAULT_NETWORK_ID}]
        networks = [{"id": DEFAULT_NETWORK_ID, "name": "Default"}]

        raw = dump_state(devices, groups, networks)
        d, g, n, ver = load_state(raw)

        self.assertEqual(ver, CURRENT_SCHEMA_VERSION)
        self.assertEqual(d, devices)
        self.assertEqual(g, groups)
        self.assertEqual(n, networks)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
