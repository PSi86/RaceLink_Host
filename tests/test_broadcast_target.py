"""Unit tests for :class:`BroadcastTarget`.

Pins:

  * Construction goes through the explicit factories — direct
    ``BroadcastTarget((...))`` works for tests but is uncommon at
    call sites.
  * Empty input raises (no silent no-op).
  * Duplicates are coalesced; order is preserved by first occurrence.
  * ``resolve_transports`` skips missing networks without raising and
    coalesces two ids that map to the same transport.
"""

from __future__ import annotations

import unittest

from racelink.transport.broadcast_target import BroadcastTarget


class _FakeNetworkRepo:
    def __init__(self, nets):
        self._items = list(nets)

    def list(self):
        return list(self._items)


class _FakeNetwork:
    def __init__(self, *, nid, gateway_mac):
        self.id = nid
        self.gateway_mac = gateway_mac


class _FakeTransport:
    def __init__(self, *, ident_mac, network_id=None):
        self.ident_mac = ident_mac
        self.network_id = network_id


class _FakeController:
    def __init__(self, *, transports, network_to_transport=None):
        self._transports = list(transports)
        self._n2t = dict(network_to_transport or {})

    @property
    def transports(self):
        return list(self._transports)

    def transport_for_network(self, network_id):
        return self._n2t.get(network_id)


class ConstructionTests(unittest.TestCase):

    def test_from_ids_preserves_order_and_deduplicates(self):
        target = BroadcastTarget.from_ids(["a", "b", "a", "c", "b"])
        self.assertEqual(target.network_ids, ("a", "b", "c"))

    def test_from_ids_rejects_empty(self):
        with self.assertRaises(ValueError):
            BroadcastTarget.from_ids([])

    def test_from_ids_rejects_only_blanks(self):
        with self.assertRaises(ValueError):
            BroadcastTarget.from_ids(["", None, ""])

    def test_single_constructs_one_element(self):
        target = BroadcastTarget.single("net-x")
        self.assertEqual(target.network_ids, ("net-x",))

    def test_single_rejects_empty_id(self):
        with self.assertRaises(ValueError):
            BroadcastTarget.single("")

    def test_all_attached_collects_from_controller(self):
        t_a = _FakeTransport(ident_mac="GW-A", network_id="net-a")
        t_b = _FakeTransport(ident_mac="GW-B", network_id="net-b")
        ctrl = _FakeController(transports=[t_a, t_b])
        target = BroadcastTarget.all_attached(ctrl)
        self.assertEqual(target.network_ids, ("net-a", "net-b"))

    def test_all_attached_skips_unbound_transports(self):
        t_a = _FakeTransport(ident_mac="GW-A", network_id="net-a")
        t_unbound = _FakeTransport(ident_mac="GW-B", network_id=None)
        ctrl = _FakeController(transports=[t_a, t_unbound])
        target = BroadcastTarget.all_attached(ctrl)
        self.assertEqual(target.network_ids, ("net-a",))

    def test_all_attached_raises_when_nothing_bound(self):
        t_unbound = _FakeTransport(ident_mac="GW-X", network_id=None)
        ctrl = _FakeController(transports=[t_unbound])
        with self.assertRaises(ValueError):
            BroadcastTarget.all_attached(ctrl)

    def test_normalises_non_string_ids(self):
        # int network ids should be coerced to str via the
        # ``__post_init__`` normalisation path.
        target = BroadcastTarget(network_ids=(1, "b"))
        self.assertEqual(target.network_ids, ("1", "b"))

    def test_immutable_and_hashable(self):
        target = BroadcastTarget.from_ids(["a", "b"])
        with self.assertRaises(Exception):
            target.network_ids = ("c",)  # type: ignore[misc]
        # Hash works → can be a dict key / set member.
        self.assertEqual(hash(target), hash(target))
        {target}


class ResolveTransportsTests(unittest.TestCase):

    def test_resolves_each_id_through_controller(self):
        t_a = _FakeTransport(ident_mac="GW-A")
        t_b = _FakeTransport(ident_mac="GW-B")
        ctrl = _FakeController(
            transports=[t_a, t_b],
            network_to_transport={"net-a": t_a, "net-b": t_b},
        )
        target = BroadcastTarget.from_ids(["net-a", "net-b"])
        self.assertEqual(target.resolve_transports(ctrl), [t_a, t_b])

    def test_skips_missing_networks_without_raising(self):
        t_a = _FakeTransport(ident_mac="GW-A")
        ctrl = _FakeController(
            transports=[t_a],
            network_to_transport={"net-a": t_a},
        )
        target = BroadcastTarget.from_ids(["net-a", "net-missing"])
        self.assertEqual(target.resolve_transports(ctrl), [t_a])

    def test_coalesces_ids_that_map_to_same_transport(self):
        t_a = _FakeTransport(ident_mac="GW-A")
        ctrl = _FakeController(
            transports=[t_a],
            network_to_transport={"net-a": t_a, "net-a-alias": t_a},
        )
        target = BroadcastTarget.from_ids(["net-a", "net-a-alias"])
        # Two distinct ids, but the controller maps both to the same
        # transport — resolve coalesces so the broadcast is not sent
        # twice on the same wire.
        self.assertEqual(target.resolve_transports(ctrl), [t_a])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
