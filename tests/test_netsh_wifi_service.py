"""Unit tests for the Windows ``netsh wlan`` host-WiFi backend.

Subprocess is never invoked: the deterministic logic (locale-independent
interface parsing, profile XML generation, iface resolution, association
detection) is exercised against captured real-world output and stubbed
helpers.
"""

import unittest

from racelink.services.netsh_wifi_service import NetshWifiService


# Captured verbatim from ``netsh wlan show interfaces`` on a German
# Windows 11 host (disconnected state) — the locale-robust parser must
# pull the adapter name out of this without matching German labels.
GERMAN_DISCONNECTED = (
    "\n"
    "Es ist 1 Schnittstelle auf dem System vorhanden:\n"
    "\n"
    "    Name                   : WLAN\n"
    "    Beschreibung            : Intel(R) Wi-Fi 6E AX211 160MHz\n"
    "    GUID                   : f4bd0bdb-ce3f-4b58-b84d-0ec5618fc0db\n"
    "    Physische Adresse       : bc:6e:e2:fa:e0:ec\n"
    "    Status                  : getrennt\n"
    "    Funkstatus           : Hardware Aktiviert\n"
    "                             Software Aktiviert\n"
)

GERMAN_CONNECTED = (
    "\n"
    "Es ist 1 Schnittstelle auf dem System vorhanden:\n"
    "\n"
    "    Name                   : WLAN\n"
    "    Beschreibung            : Intel(R) Wi-Fi 6E AX211 160MHz\n"
    "    Status                  : verbunden\n"
    "    SSID                    : WLED_RaceLink_AP\n"
    "    BSSID                   : be:6e:e2:fa:e0:ed\n"
)


class ParseIfaceTests(unittest.TestCase):
    def test_parses_german_adapter_name(self):
        self.assertEqual(
            NetshWifiService._parse_iface_names(GERMAN_DISCONNECTED), ["WLAN"]
        )

    def test_empty_output_yields_no_ifaces(self):
        self.assertEqual(NetshWifiService._parse_iface_names(""), [])


class ResolveIfaceTests(unittest.TestCase):
    def test_linux_default_is_ignored_in_favor_of_detected(self):
        svc = NetshWifiService()
        svc._detect_ifaces = lambda: ["WLAN"]
        self.assertEqual(svc._resolve_iface("wlan0"), "WLAN")

    def test_explicit_windows_name_is_honored(self):
        svc = NetshWifiService()
        svc._detect_ifaces = lambda: ["Wi-Fi"]
        self.assertEqual(svc._resolve_iface("Wi-Fi"), "Wi-Fi")


class ProfileXmlTests(unittest.TestCase):
    def test_wpa2psk_profile_contains_ssid_and_key(self):
        xml = NetshWifiService._profile_xml("WLED_RaceLink_AP", "wled1234")
        self.assertIn("<authentication>WPA2PSK</authentication>", xml)
        self.assertIn("<name>WLED_RaceLink_AP</name>", xml)
        self.assertIn("<keyMaterial>wled1234</keyMaterial>", xml)

    def test_open_profile_when_no_password(self):
        xml = NetshWifiService._profile_xml("OpenAP", "")
        self.assertIn("<authentication>open</authentication>", xml)
        self.assertNotIn("sharedKey", xml)

    def test_special_chars_are_xml_escaped(self):
        xml = NetshWifiService._profile_xml("A&B<C", "p&w")
        self.assertIn("A&amp;B&lt;C", xml)
        self.assertIn("p&amp;w", xml)
        self.assertNotIn("A&B<C", xml)


class RadioAndAssocTests(unittest.TestCase):
    def test_radio_enabled_true_when_interface_present(self):
        svc = NetshWifiService()
        svc._show_interfaces_raw = lambda: GERMAN_DISCONNECTED
        self.assertTrue(svc.radio_enabled())

    def test_radio_enabled_false_when_no_interface(self):
        svc = NetshWifiService()
        svc._show_interfaces_raw = lambda: ""
        self.assertFalse(svc.radio_enabled())

    def test_is_associated_matches_ssid_value_not_label(self):
        svc = NetshWifiService()
        svc._show_interfaces_raw = lambda: GERMAN_CONNECTED
        self.assertTrue(svc._is_associated("WLED_RaceLink_AP", "WLAN"))
        self.assertFalse(svc._is_associated("SomeOtherAP", "WLAN"))


class AddProfileTests(unittest.TestCase):
    def test_deletes_existing_profile_before_add(self):
        svc = NetshWifiService()
        calls = []

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_netsh(args, timeout_s=20.0):
            calls.append(list(args))
            return _P()

        svc._netsh = fake_netsh
        svc._add_profile("WLED-AP", "wled1234", "WLAN")
        # The profile collision fix: a delete must precede the add so a
        # pre-existing all-user/group-policy profile can't block it.
        self.assertEqual(calls[0][:3], ["wlan", "delete", "profile"])
        self.assertIn("name=WLED-AP", calls[0])
        self.assertEqual(calls[1][:3], ["wlan", "add", "profile"])


class ConnectApTests(unittest.TestCase):
    def test_connect_returns_associated_ssid(self):
        svc = NetshWifiService()
        added = {}
        svc._add_profile = lambda ssid, pw, iface: added.update(ssid=ssid, pw=pw)
        svc._connect = lambda ssid, iface: None
        svc._disconnect = lambda iface: None
        svc._location_services_blocked = lambda: False
        svc._show_interfaces_raw = lambda: GERMAN_CONNECTED
        got = svc.connect_ap("WLED_RaceLink_AP", "wled1234", iface="wlan0", timeout_s=5)
        self.assertEqual(got, "WLED_RaceLink_AP")
        self.assertEqual(added["ssid"], "WLED_RaceLink_AP")

    def test_no_candidates_raises(self):
        svc = NetshWifiService()
        with self.assertRaises(RuntimeError):
            svc.connect_ap([], "pw")

    def test_bssid_hints_accepted_and_ignored(self):
        svc = NetshWifiService()
        svc._add_profile = lambda *a, **k: None
        svc._connect = lambda *a, **k: None
        svc._disconnect = lambda iface: None
        svc._location_services_blocked = lambda: False
        svc._show_interfaces_raw = lambda: GERMAN_CONNECTED
        # avoid_bssid / bssid must not raise (API parity with nmcli backend)
        got = svc.connect_ap(
            ["WLED_RaceLink_AP"], "wled1234", iface="WLAN",
            bssid="AA:BB:CC:DD:EE:FF", avoid_bssid="11:22:33:44:55:66", timeout_s=5,
        )
        self.assertEqual(got, "WLED_RaceLink_AP")

    def test_associated_candidate_picks_matching_ssid(self):
        svc = NetshWifiService()
        svc._show_interfaces_raw = lambda: GERMAN_CONNECTED
        self.assertEqual(
            svc._associated_candidate(["WLED-AP", "WLED_RaceLink_AP"]),
            "WLED_RaceLink_AP",
        )
        self.assertEqual(svc._associated_candidate(["NotHere"]), "")

    def test_active_bssid_is_empty(self):
        self.assertEqual(NetshWifiService().active_bssid("WLAN"), "")


class LocationServicesTests(unittest.TestCase):
    def test_is_location_block_detects_token(self):
        self.assertTrue(
            NetshWifiService._is_location_block(
                "Run start ms-settings:privacy-location to enable"
            )
        )
        self.assertFalse(NetshWifiService._is_location_block("    SSID : WLED-AP"))

    def test_connect_ap_fails_fast_when_location_off(self):
        svc = NetshWifiService()
        svc._detect_ifaces = lambda: ["WLAN"]
        svc._location_services_blocked = lambda: True
        with self.assertRaises(RuntimeError) as ctx:
            svc.connect_ap(["WLED-AP"], "wled1234", timeout_s=5)
        self.assertIn("Location Services", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
