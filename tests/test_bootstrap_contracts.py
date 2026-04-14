import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "racelink" / "integrations" / "rotorhazard" / "plugin.py"
CONTROLLER_PATH = ROOT / "controller.py"
API_PATH = ROOT / "racelink" / "web" / "api.py"


class BootstrapContractTests(unittest.TestCase):
    def test_rotorhazard_plugin_wires_expected_services(self):
        tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"), filename=str(PLUGIN_PATH))
        service_keys = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "RaceLinkApp":
                continue
            for kw in node.keywords:
                if kw.arg != "services" or not isinstance(kw.value, ast.Dict):
                    continue
                for key_node in kw.value.keys:
                    if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                        service_keys.add(key_node.value)

        expected = {
            "config",
            "control",
            "discovery",
            "gateway",
            "host_wifi",
            "ota",
            "presets",
            "startblock",
            "status",
            "stream",
            "sync",
        }
        self.assertTrue(expected.issubset(service_keys), service_keys)

    def test_controller_no_longer_contains_dead_startblock_paths(self):
        source = CONTROLLER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("startblock_use_current_heat", source)
        self.assertNotIn("targetDevice has no STARTBLOCK capability", source)
        self.assertNotIn("Liefert die Startblock", source)

    def test_web_api_delegates_long_workflows_to_services(self):
        source = API_PATH.read_text(encoding="utf-8")
        self.assertIn("ota_workflows.download_presets", source)
        self.assertIn("ota_workflows.run_firmware_update", source)
        self.assertIn("specials_service.resolve_option", source)
        self.assertIn("specials_service.resolve_action", source)
        self.assertNotIn("host_wifi_service.connect_profile(", source)
        self.assertNotIn("ota_service.wait_for_expected_node(", source)


if __name__ == "__main__":
    unittest.main()
