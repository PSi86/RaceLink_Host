import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "racelink" / "integrations" / "rotorhazard" / "plugin.py"
CONTROLLER_PATH = ROOT / "controller.py"
API_PATH = ROOT / "racelink" / "web" / "api.py"
STANDALONE_WEBAPP_PATH = ROOT / "racelink" / "integrations" / "standalone" / "webapp.py"


class BootstrapContractTests(unittest.TestCase):
    def test_rotorhazard_plugin_uses_host_runtime_factory(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertIn("create_runtime(", source)
        self.assertIn("presets_apply_options=rh_adapter.apply_presets_options", source)
        self.assertIn("integrations={\"rotorhazard\": rhapi", source)
        self.assertNotIn("RaceLinkApp(", source)

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

    def test_standalone_webapp_uses_shared_web_registration_entry(self):
        source = STANDALONE_WEBAPP_PATH.read_text(encoding="utf-8")
        self.assertIn("register_racelink_web", source)
        self.assertIn("RaceLinkWebRuntime", source)
        self.assertNotIn("register_rl_blueprint(", source)


if __name__ == "__main__":
    unittest.main()
