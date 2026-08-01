import unittest
import sys
import types
from unittest import mock


serial_stub = types.ModuleType("serial")


class _FakeSerial:
    def __init__(self, *args, **kwargs):
        self.baudrate = None
        self.timeout = None
        self.port = None
        self.is_open = False


serial_stub.Serial = _FakeSerial
serial_stub.SerialException = Exception


class _FakeSerialTimeoutException(Exception):
    """Mirrors pyserial's subclassing so the production code's
    ``except SerialTimeoutException`` / ``except SerialException``
    ordering behaves the same under the stub."""


serial_stub.SerialTimeoutException = _FakeSerialTimeoutException
serial_tools_stub = types.ModuleType("serial.tools")
serial_list_ports_stub = types.ModuleType("serial.tools.list_ports")
serial_list_ports_stub.comports = lambda: []
serial_tools_stub.list_ports = serial_list_ports_stub
serial_stub.tools = serial_tools_stub

sys.modules.setdefault("serial", serial_stub)
sys.modules.setdefault("serial.tools", serial_tools_stub)
sys.modules.setdefault("serial.tools.list_ports", serial_list_ports_stub)

from racelink.transport import gateway_serial as _gateway_serial_mod
from racelink.transport.gateway_serial import GatewaySerialTransport, SendOutcome
from racelink.transport.gateway_events import (
    GATEWAY_STATE_IDLE,
    GATEWAY_STATE_RX_WINDOW,
    GATEWAY_STATE_UNKNOWN,
)


class GatewaySerialTransportSendTests(unittest.TestCase):
    """Smoke-tests for the synchronous send path (Batch B).

    The transport's wire-level `_send_m2n` blocks until the gateway's
    matching outcome event lands; tests stub it to keep the high-level
    helpers (send_stream etc.) exercised without a real USB.
    """

    def test_send_stream_sends_raw_payload_without_host_ctrl(self):
        transport = GatewaySerialTransport(port="COM1")
        calls = []

        def fake_send(type_full, recv3, body=b""):
            calls.append(
                {
                    "type_full": type_full,
                    "recv3": recv3,
                    "body": body,
                }
            )
            return SendOutcome.success()

        transport._send_m2n = fake_send

        outcome = transport.send_stream(recv3=b"\xAA\xBB\xCC", payload=b"\x01\x02")

        self.assertTrue(bool(outcome))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["recv3"], b"\xAA\xBB\xCC")
        self.assertEqual(calls[0]["body"], b"\x01\x02")


class GatewayStateMirrorTests(unittest.TestCase):
    """The transport's state mirror is the host's source of pill truth.

    Pre-Batch-B this lived behind the EV_RX_WINDOW_OPEN/CLOSED pair (idempotent
    OPEN/CLOSE counter). Batch B consolidated both events into EV_STATE_CHANGED
    with a state-byte body; the test set follows the same shape — verifies
    the mirror updates from each event, with metadata preserved for
    RX_WINDOW.
    """

    def test_initial_state_is_unknown_until_first_event(self):
        transport = GatewaySerialTransport(port="COM1")
        self.assertEqual(transport.gateway_state_byte, GATEWAY_STATE_UNKNOWN)
        self.assertEqual(transport.gateway_state_name, "UNKNOWN")
        self.assertEqual(transport.gateway_state_metadata_ms, 0)

    def test_state_changed_idle_updates_mirror(self):
        transport = GatewaySerialTransport(port="COM1")
        transport._update_gateway_state(GATEWAY_STATE_IDLE, 0)
        self.assertEqual(transport.gateway_state_byte, GATEWAY_STATE_IDLE)
        self.assertEqual(transport.gateway_state_name, "IDLE")
        self.assertEqual(transport.gateway_state_metadata_ms, 0)

    def test_state_changed_rx_window_carries_metadata(self):
        transport = GatewaySerialTransport(port="COM1")
        transport._update_gateway_state(GATEWAY_STATE_RX_WINDOW, 1500)
        self.assertEqual(transport.gateway_state_byte, GATEWAY_STATE_RX_WINDOW)
        self.assertEqual(transport.gateway_state_name, "RX_WINDOW")
        self.assertEqual(transport.gateway_state_metadata_ms, 1500)

    def test_parse_state_event_body_handles_short_bodies(self):
        # 1-byte body (state-only, no metadata)
        sb, ms = GatewaySerialTransport._parse_state_event_body(bytes([0x00]))
        self.assertEqual(sb, GATEWAY_STATE_IDLE)
        self.assertEqual(ms, 0)
        # 3-byte body (state + LE16 metadata)
        sb, ms = GatewaySerialTransport._parse_state_event_body(bytes([0x02, 0xE8, 0x03]))  # 1000 ms
        self.assertEqual(sb, GATEWAY_STATE_RX_WINDOW)
        self.assertEqual(ms, 1000)
        # empty body falls back to UNKNOWN sentinel
        sb, ms = GatewaySerialTransport._parse_state_event_body(b"")
        self.assertEqual(sb, GATEWAY_STATE_UNKNOWN)
        self.assertEqual(ms, 0)


class EnumerateAllWedgedPortTests(unittest.TestCase):
    """A USB device that opens but never reads must not stop the walk.

    Bench failure this pins: an ESP32's built-in USB-Serial/JTAG unit
    enumerated as ``/dev/ttyACM0``. It accepted the connection and then
    never drained the probe write, so with pyserial's default
    ``write_timeout=None`` the write blocked forever. Because ttyACM0
    sorts before ttyUSB0, the loop never reached the real gateway and
    the host came up with no RF transport at all — with nothing in the
    log, since the port was skipped silently.
    """

    class _Port:
        def __init__(self, device, description="USB Serial"):
            self.device = device
            self.description = description

    def _run_enumerate(self, wedged_devices):
        opened: list[str] = []

        class _ProbeSerial:
            def __init__(self, *args, **kwargs):
                self.baudrate = None
                self.timeout = kwargs.get("timeout")
                self.write_timeout = kwargs.get("write_timeout")
                self.port = None
                self.is_open = False

            def open(self):
                self.is_open = True
                opened.append(self.port)

            def close(self):
                self.is_open = False

            def reset_input_buffer(self):
                pass

            def write(self, _payload):
                if self.port in wedged_devices:
                    # What pyserial raises once write_timeout elapses.
                    raise _gateway_serial_mod.serial.SerialTimeoutException(
                        f"Write timeout on {self.port}"
                    )

            def read(self, _n):
                return b"RaceLink_Gateway_v4" + b"9C:13:9E:9E:1C:10"

        ports = [self._Port("/dev/ttyACM0"), self._Port("/dev/ttyUSB0")]
        mod_serial = _gateway_serial_mod.serial
        with mock.patch.object(mod_serial, "Serial", _ProbeSerial),                 mock.patch.object(
                    mod_serial.tools.list_ports, "comports", lambda: ports):
            found = GatewaySerialTransport.enumerate_all()
        return found, opened

    def test_wedged_port_does_not_hide_the_real_gateway(self):
        found, opened = self._run_enumerate({"/dev/ttyACM0"})

        # The walk continued past the wedged port…
        self.assertIn("/dev/ttyUSB0", opened)
        # …and the real gateway was still found.
        self.assertEqual(found, [("/dev/ttyUSB0", "9C:13:9E:9E:1C:10")])

    def test_wedged_port_is_reported_not_swallowed(self):
        """The operator sees "gateway not detected"; without the port
        name in the log there is nothing to act on."""
        with self.assertLogs("racelink_transport", level="WARNING") as captured:
            self._run_enumerate({"/dev/ttyACM0"})

        joined = "\n".join(captured.output)
        self.assertIn("/dev/ttyACM0", joined)
        self.assertIn("never read the probe", joined)

    def test_probe_sets_a_write_timeout(self):
        """The guard itself: without an explicit write_timeout pyserial
        blocks forever and none of the above can happen."""
        captured: list = []

        class _RecordingSerial(_FakeSerial):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured.append(kwargs)
                self.write_timeout = kwargs.get("write_timeout")

            def open(self):
                self.is_open = True

            def close(self):
                self.is_open = False

            def reset_input_buffer(self):
                pass

            def write(self, _payload):
                pass

            def read(self, _n):
                return b""

        ports = [self._Port("/dev/ttyUSB0")]
        mod_serial = _gateway_serial_mod.serial
        with mock.patch.object(mod_serial, "Serial", _RecordingSerial),                 mock.patch.object(
                    mod_serial.tools.list_ports, "comports", lambda: ports):
            GatewaySerialTransport.enumerate_all()

        self.assertTrue(captured)
        self.assertIsNotNone(captured[0].get("write_timeout"))
        self.assertGreater(captured[0]["write_timeout"], 0)


if __name__ == "__main__":
    unittest.main()
