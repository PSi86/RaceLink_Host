"""Sync fan-out tests (Stage 3 Part G).

Pins:

  * Broadcast ``OPC_SYNC`` (``recv3=FFFFFF``) fires on every attached
    transport. This is the autosync timer's path — every network's
    devices need a tick on their own radio.
  * Unicast sync (``recv3 != FFFFFF``) routes via the target
    device's ``transport_for_device`` resolution.
  * Fan-out tolerates one transport raising — the sibling still
    gets its packet.
  * ``trigger_armed=True`` propagates to every transport's send.
"""

from __future__ import annotations

import unittest

from racelink.domain.models import RL_Device, RL_Network


class _FakeTransport:
    def __init__(self, ident_mac, *, raise_on_send=False):
        self.ident_mac = ident_mac
        self.raise_on_send = raise_on_send
        self.sync_calls: list[dict] = []

    def send_sync(self, *, recv3, ts24, brightness, flags):
        if self.raise_on_send:
            raise RuntimeError(f"simulated USB hiccup on {self.ident_mac}")
        self.sync_calls.append({
            "recv3": bytes(recv3), "ts24": int(ts24),
            "brightness": int(brightness), "flags": int(flags),
        })


class _FakeNetworkRepository:
    def __init__(self, nets):
        self._items = list(nets)

    def list(self):
        return list(self._items)

    def get_by_id(self, network_id):
        for n in self._items:
            if str(getattr(n, "id", "") or "") == str(network_id or ""):
                return n
        return None

    def get_by_gateway_mac(self, gateway_mac):
        if not gateway_mac:
            return None
        target = str(gateway_mac).upper()
        for n in self._items:
            if str(getattr(n, "gateway_mac", "") or "").upper() == target:
                return n
        return None


class _FakeDeviceRepository:
    def __init__(self, devs):
        self._items = list(devs)

    def list(self):
        return list(self._items)

    def get_by_addr(self, addr):
        target = str(addr or "").upper()
        if not target:
            return None
        for d in self._items:
            addr_up = str(getattr(d, "addr", "") or "").upper()
            if addr_up == target:
                return d
            # Mirror the production repo's last-3-bytes (6-char hex)
            # lookup used by ``recv3``-driven send paths.
            if len(target) == 6 and addr_up.endswith(target):
                return d
        return None


class _FakeController:
    def __init__(self, *, networks, devices, transports):
        self.network_repository = _FakeNetworkRepository(networks)
        self.device_repository = _FakeDeviceRepository(devices)
        self._transports = list(transports)

    @property
    def transports(self):
        return list(self._transports)

    @property
    def transport(self):
        return self._transports[0] if self._transports else None

    def getDeviceFromAddress(self, mac):
        return self.device_repository.get_by_addr(mac)

    def transport_for_network(self, network_id):
        net = self.network_repository.get_by_id(network_id)
        if net is None or not getattr(net, "gateway_mac", None):
            return self._transports[0] if len(self._transports) == 1 else None
        target = str(net.gateway_mac).upper()
        for t in self._transports:
            if str(getattr(t, "ident_mac", "") or "").upper() == target:
                return t
        return None

    def transport_for_device(self, addr):
        dev = self.device_repository.get_by_addr(addr)
        net_id = getattr(dev, "network_id", None) if dev is not None else None
        return self.transport_for_network(net_id)


def _make_gateway_service(controller):
    from racelink.services.gateway_service import GatewayService
    return GatewayService(controller)


def _device(mac, *, network_id):
    d = RL_Device(mac, dev_type=0, name=mac)
    d.network_id = network_id
    return d


class BroadcastSyncFanOutTests(unittest.TestCase):

    def test_broadcast_sends_on_every_transport(self):
        t_a = _FakeTransport("GW-A")
        t_b = _FakeTransport("GW-B")
        ctrl = _FakeController(
            networks=[
                RL_Network(name="A", gateway_mac="GW-A"),
                RL_Network(name="B", gateway_mac="GW-B"),
            ],
            devices=[], transports=[t_a, t_b],
        )
        gw = _make_gateway_service(ctrl)

        gw.send_sync(ts24=0x123456, brightness=0x40)

        self.assertEqual(len(t_a.sync_calls), 1)
        self.assertEqual(len(t_b.sync_calls), 1)
        self.assertEqual(t_a.sync_calls[0]["recv3"], b"\xFF\xFF\xFF")
        self.assertEqual(t_a.sync_calls[0]["ts24"], 0x123456)
        self.assertEqual(t_a.sync_calls[0]["brightness"], 0x40)
        # Default trigger_armed=False → flags=0.
        self.assertEqual(t_a.sync_calls[0]["flags"], 0)
        self.assertEqual(t_b.sync_calls[0]["ts24"], 0x123456)

    def test_trigger_armed_propagates_to_all_transports(self):
        t_a = _FakeTransport("GW-A")
        t_b = _FakeTransport("GW-B")
        ctrl = _FakeController(
            networks=[], devices=[], transports=[t_a, t_b],
        )
        gw = _make_gateway_service(ctrl)

        gw.send_sync(ts24=0, brightness=0, trigger_armed=True)

        # SYNC_FLAG_TRIGGER_ARMED = 0x01 — every transport's send gets it.
        self.assertEqual(t_a.sync_calls[0]["flags"] & 0x01, 0x01)
        self.assertEqual(t_b.sync_calls[0]["flags"] & 0x01, 0x01)

    def test_one_transport_failure_does_not_abort_others(self):
        t_a = _FakeTransport("GW-A", raise_on_send=True)
        t_b = _FakeTransport("GW-B")
        ctrl = _FakeController(
            networks=[], devices=[], transports=[t_a, t_b],
        )
        gw = _make_gateway_service(ctrl)

        # Should not raise; the second transport still gets the sync.
        gw.send_sync(ts24=42, brightness=0)
        self.assertEqual(t_a.sync_calls, [])
        self.assertEqual(len(t_b.sync_calls), 1)

    def test_single_transport_path_byte_identical(self):
        t_a = _FakeTransport("GW-A")
        ctrl = _FakeController(
            networks=[RL_Network(name="A", gateway_mac="GW-A")],
            devices=[], transports=[t_a],
        )
        gw = _make_gateway_service(ctrl)

        gw.send_sync(ts24=1, brightness=2)
        self.assertEqual(len(t_a.sync_calls), 1)
        self.assertEqual(t_a.sync_calls[0]["recv3"], b"\xFF\xFF\xFF")


class UnicastSyncRoutingTests(unittest.TestCase):

    def test_unicast_routes_via_devices_network(self):
        net_a = RL_Network(name="A", gateway_mac="GW-A")
        net_b = RL_Network(name="B", gateway_mac="GW-B")
        dev_b = _device("AABBCC222222", network_id=net_b.id)
        t_a = _FakeTransport("GW-A")
        t_b = _FakeTransport("GW-B")
        ctrl = _FakeController(
            networks=[net_a, net_b], devices=[dev_b],
            transports=[t_a, t_b],
        )
        gw = _make_gateway_service(ctrl)

        gw.send_sync(ts24=99, brightness=0, recv3=b"\x22\x22\x22")

        # Only the device's network transport received the send.
        self.assertEqual(t_a.sync_calls, [])
        self.assertEqual(len(t_b.sync_calls), 1)
        self.assertEqual(t_b.sync_calls[0]["recv3"], b"\x22\x22\x22")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
