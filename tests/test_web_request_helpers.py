import unittest
import importlib.util
import pathlib


def _load_request_helpers():
    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "racelink" / "web" / "request_helpers.py"
    spec = importlib.util.spec_from_file_location("request_helpers_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class WebRequestHelpersTests(unittest.TestCase):
    def test_parse_recv3_from_addr_uses_last_three_bytes(self):
        helpers = _load_request_helpers()
        self.assertEqual(helpers.parse_recv3_from_addr("AA:BB:CC:DD:EE:FF"), bytes.fromhex("DDEEFF"))

    def test_parse_wifi_options_applies_defaults(self):
        class FakeOTA:
            @staticmethod
            def wled_base_url(raw):
                return (raw or "http://4.3.2.1").rstrip("/")

        helpers = _load_request_helpers()
        parsed = helpers.parse_wifi_options({}, FakeOTA())
        self.assertEqual(parsed["base_url"], "http://4.3.2.1")
        self.assertEqual(parsed["iface"], "wlan0")
        self.assertTrue(parsed["host_wifi_enable"])


if __name__ == "__main__":
    unittest.main()
