"""Service-layer tests for the per-node OPC_RF_CONFIG push (Stage 1.5 PR-5).

set_node_rf_config wraps the existing send_and_wait_with_retries
pipeline with an OPC_RF_CONFIG body. query_node_rf_config does the
read-back via OPC_GET_RF_CONFIG. Both are exercised against the
FakeTransport / FakeController scaffolding used by the broader
gateway_service test suite.
"""

from __future__ import annotations

import unittest

from racelink.services.gateway_service import GatewayService
from racelink.transport import LP

# Reuse the existing FakeTransport / FakeController scaffolding instead
# of duplicating it.
from tests.test_gateway_service import FakeController, FakeTransport


_CANONICAL_CFG = {
    "freq_hz":      867_700_000,
    "bw_khz_x10":   1250,
    "sf":           7,
    "cr_den":       5,
    "sync_word":    0x12,
    "tx_power_dbm": 14,
    "preamble":     8,
}


def _augmented_transport() -> FakeTransport:
    """FakeTransport with the two new send hooks the service calls."""
    transport = FakeTransport()
    transport.sent_rf_config = []
    transport.sent_get_rf_config = []
    # Patch in the methods the service expects. Defaults emit no events;
    # individual tests override these to simulate ACK / GET-reply paths.
    transport.send_rf_config = lambda recv3, rf_config: transport.sent_rf_config.append({
        "recv3": recv3, "rf_config": rf_config,
    })
    transport.send_get_rf_config_to_node = lambda recv3: transport.sent_get_rf_config.append({
        "recv3": recv3,
    })
    return transport


class SetNodeRfConfigTests(unittest.TestCase):

    def _service(self):
        controller = FakeController()
        controller.transport = _augmented_transport()
        service = GatewayService(controller)
        return controller, service

    def test_ack_ok_returns_success(self):
        controller, service = self._service()
        transport = controller.transport

        # When send_rf_config fires, synthesise the node's ACK_OK so the
        # blocking send_and_wait_with_retries unblocks immediately.
        def _send_and_ack(recv3, rf_config):
            transport.sent_rf_config.append({"recv3": recv3, "rf_config": rf_config})
            transport.emit({
                "opc": LP.OPC_ACK,
                "ack_of": LP.OPC_RF_CONFIG,
                "ack_status": 0,
                "sender3": bytes.fromhex("DDEEFF"),
            })
        transport.send_rf_config = _send_and_ack

        result = service.set_node_rf_config("AABBCCDDEEFF", _CANONICAL_CFG)

        self.assertTrue(result["ok"])
        self.assertEqual(result["ack_status"], 0)
        self.assertEqual(len(transport.sent_rf_config), 1)
        sent = transport.sent_rf_config[0]
        self.assertEqual(sent["recv3"], b"\xDD\xEE\xFF")
        self.assertEqual(sent["rf_config"], _CANONICAL_CFG)

    def test_nack_bad_len_returns_failure_with_ack_status(self):
        controller, service = self._service()
        transport = controller.transport

        def _send_and_nack(recv3, rf_config):
            transport.sent_rf_config.append({"recv3": recv3, "rf_config": rf_config})
            transport.emit({
                "opc": LP.OPC_ACK,
                "ack_of": LP.OPC_RF_CONFIG,
                "ack_status": 2,  # ACK_BAD_LEN (firmware rejection)
                "sender3": bytes.fromhex("DDEEFF"),
            })
        transport.send_rf_config = _send_and_nack

        result = service.set_node_rf_config("AABBCCDDEEFF", _CANONICAL_CFG)

        self.assertFalse(result["ok"])
        self.assertEqual(result["ack_status"], 2)

    def test_timeout_returns_error(self):
        controller, service = self._service()
        # send_rf_config emits nothing; send_and_wait_with_retries
        # spins until the per-attempt timeout fires. Use a tiny
        # timeout so the test stays fast.
        result = service.set_node_rf_config(
            "AABBCCDDEEFF", _CANONICAL_CFG, timeout_s=0.05,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "timeout")

    def test_broadcast_rejected_without_hitting_transport(self):
        # 12-char hex sentinel for broadcast (FF:FF:FF as last 3). We
        # avoid the broadcast path by design — the firmware also
        # rejects it (OPC_CONFIG-style broadcast forbidden), but a
        # host send would brick every reachable node simultaneously.
        controller, service = self._service()
        result = service.set_node_rf_config("000000FFFFFF", _CANONICAL_CFG)
        self.assertFalse(result["ok"])
        self.assertIn("broadcast", result["error"])
        self.assertEqual(controller.transport.sent_rf_config, [])

    def test_short_mac_rejected(self):
        controller, service = self._service()
        result = service.set_node_rf_config("AABBCC", _CANONICAL_CFG)
        self.assertFalse(result["ok"])
        self.assertIn("12 hex chars", result["error"])

    def test_transport_unavailable_returns_error(self):
        controller, service = self._service()
        controller.transport = None
        result = service.set_node_rf_config("AABBCCDDEEFF", _CANONICAL_CFG)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "transport unavailable")


class QueryNodeRfConfigTests(unittest.TestCase):

    def _service(self):
        controller = FakeController()
        controller.transport = _augmented_transport()
        service = GatewayService(controller)
        return controller, service

    def test_get_reply_returns_rf_config(self):
        controller, service = self._service()
        transport = controller.transport

        def _send_and_reply(recv3):
            transport.sent_get_rf_config.append({"recv3": recv3})
            # GET_RF_CONFIG reply is RESP_SPECIFIC with the same opcode
            # (N2M direction) carrying an unpacked rf_config dict.
            transport.emit({
                "opc": LP.OPC_GET_RF_CONFIG,
                "reply": "GET_RF_CONFIG_REPLY",
                "rf_config": _CANONICAL_CFG,
                "sender3": bytes.fromhex("DDEEFF"),
            })
        transport.send_get_rf_config_to_node = _send_and_reply

        result = service.query_node_rf_config("AABBCCDDEEFF")
        self.assertTrue(result["ok"])
        self.assertEqual(result["rf_config"], _CANONICAL_CFG)

    def test_timeout_returns_error(self):
        controller, service = self._service()
        result = service.query_node_rf_config(
            "AABBCCDDEEFF", timeout_s=0.05,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "timeout")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
