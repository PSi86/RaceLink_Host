"""Tests for the host-side Ethernet autosync service.

The host drives the periodic OPC_SYNC on Ethernet networks (RF networks
get theirs from the gateway). These pin:

  * ``tick`` targets ONLY Ethernet transports (RF networks are left alone).
  * autosync form: ``trigger_armed=False``, ``brightness=0``, and ``ts24``
    masked to 24 bits.
  * a no-Ethernet deployment is a no-op (returns 0, no send).
"""

from __future__ import annotations

import types
import unittest

from racelink.services.eth_autosync_service import EthAutosyncService


class _FakeSyncService:
    def __init__(self):
        self.calls = []

    def send_sync(self, ts24, brightness, *, recv3=b"\xFF\xFF\xFF",
                  trigger_armed=False, target=None):
        self.calls.append({
            "ts24": ts24,
            "brightness": brightness,
            "trigger_armed": trigger_armed,
            "target": target,
        })


def _transport(kind, network_id):
    return types.SimpleNamespace(kind=kind, network_id=network_id)


def _controller(transports, sync_service):
    return types.SimpleNamespace(transports=transports, sync_service=sync_service)


class EthAutosyncTickTests(unittest.TestCase):
    def test_tick_targets_only_ethernet_networks(self):
        sync = _FakeSyncService()
        controller = _controller(
            [
                _transport("rf", "net-rf"),
                _transport("ethernet", "net-eth"),
                _transport("rf", "net-rf-2"),
            ],
            sync,
        )
        svc = EthAutosyncService(controller, clock_ms=lambda: 0x1ABCDEF)

        n = svc.tick()

        self.assertEqual(n, 1)
        self.assertEqual(len(sync.calls), 1)
        call = sync.calls[0]
        # Autosync form: no arm, brightness ignored (0), ts24 masked to 24 bits.
        self.assertFalse(call["trigger_armed"])
        self.assertEqual(call["brightness"], 0)
        self.assertEqual(call["ts24"], 0x1ABCDEF & 0xFFFFFF)
        # Only the Ethernet network is addressed — RF networks are skipped.
        self.assertEqual(call["target"].network_ids, ("net-eth",))

    def test_tick_addresses_every_ethernet_network(self):
        sync = _FakeSyncService()
        controller = _controller(
            [
                _transport("ethernet", "net-eth-a"),
                _transport("ethernet", "net-eth-b"),
            ],
            sync,
        )
        svc = EthAutosyncService(controller)

        n = svc.tick()

        self.assertEqual(n, 2)
        self.assertEqual(
            set(sync.calls[0]["target"].network_ids),
            {"net-eth-a", "net-eth-b"},
        )

    def test_tick_is_noop_without_ethernet(self):
        sync = _FakeSyncService()
        controller = _controller([_transport("rf", "net-rf")], sync)
        svc = EthAutosyncService(controller)

        self.assertEqual(svc.tick(), 0)
        self.assertEqual(sync.calls, [])

    def test_tick_is_noop_with_no_transports(self):
        sync = _FakeSyncService()
        controller = _controller([], sync)
        svc = EthAutosyncService(controller)

        self.assertEqual(svc.tick(), 0)
        self.assertEqual(sync.calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
