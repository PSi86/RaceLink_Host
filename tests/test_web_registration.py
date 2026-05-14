import contextlib
import importlib
import sys
import unittest
from pathlib import Path

from tests._flask_stub import install_flask
from racelink.domain import RL_DeviceGroup


def _ensure_flask_stub():
    flask = install_flask()
    # ``test_web_registration`` reads payloads as raw dicts (its
    # ``jsonify`` stub is the identity function); the shared default
    # wraps args+kwargs which would break the assertions below.
    flask.jsonify = lambda payload: payload


class _FakeApp:
    def __init__(self):
        self.blueprints = []

    def register_blueprint(self, blueprint):
        self.blueprints.append(blueprint)


class _FakeRuntime:
    def __init__(self):
        self.rl_instance = type("RL", (), {"uiPresetList": [{"value": "01", "label": "Red"}]})()
        self.state_repository = None
        self.rl_devicelist = []
        self.rl_grouplist = [RL_DeviceGroup("Group 1")]
        self.services = {
            "host_wifi": type("HostWifi", (), {"wifi_interfaces": staticmethod(lambda: ["wlan0"])})(),
            "ota": type("OTA", (), {})(),
            "presets": type(
                "Presets",
                (),
                {
                    "ensure_loaded": staticmethod(lambda: True),
                    "list_files": staticmethod(lambda: []),
                    "get_current_name": staticmethod(lambda: ""),
                    "preset_path_for_name": staticmethod(lambda name: None),
                },
            )(),
        }
        self.RL_DeviceGroup = RL_DeviceGroup
        self.logger = None
        self.option_getter = lambda _key, default=None: default
        self.translator = lambda text: text
        self.blueprint_registrar = None

    def option(self, key, default=None):
        return self.option_getter(key, default)

    def translate(self, text):
        return self.translator(text)


class WebRegistrationTests(unittest.TestCase):
    def setUp(self):
        _ensure_flask_stub()
        for name in ("racelink.web", "racelink.web.blueprint", "racelink.web.api", "racelink.web.sse"):
            sys.modules.pop(name, None)
        self.web = importlib.import_module("racelink.web")

    def test_register_racelink_web_mounts_prefix_aware_blueprint(self):
        app = _FakeApp()
        runtime = _FakeRuntime()

        bp = self.web.register_racelink_web(app, runtime, url_prefix="/shared-ui")

        self.assertEqual(len(app.blueprints), 1)
        self.assertIs(app.blueprints[0], bp)
        self.assertEqual(bp.kwargs.get("url_prefix"), "/shared-ui")
        self.assertEqual(bp.kwargs.get("static_url_path"), "/static")
        self.assertIn(("/", ("GET",)), bp.routes)
        self.assertIn(("/api/devices", ("GET",)), bp.routes)
        self.assertIn(("/api/events", ("GET",)), bp.routes)

    def test_asset_dirs_resolve_to_existing_paths(self):
        # The SPA shell is rendered via render_template_string, so no
        # Jinja templates remain — ``_resolve_asset_dirs`` returns only
        # ``static_dir`` (resolves to ``racelink/static``) for the
        # blueprint's ``static_folder`` hand-off.
        blueprint = importlib.import_module("racelink.web.blueprint")
        static_dir = blueprint._resolve_asset_dirs()

        self.assertTrue(Path(static_dir).is_dir())
        self.assertEqual(Path(static_dir).name, "static")
        self.assertEqual(Path(static_dir).parent.name, "racelink")
        # Sanity: the SPA shell shipped by Vite must be present.
        self.assertTrue((Path(static_dir) / "dist" / "index.html").is_file())

    def test_root_render_injects_base_and_static_paths(self):
        # Phase 1 PoC: the route now invokes render_template_string with
        # the SPA shell's HTML source. The stub captures kwargs verbatim
        # so we can still assert that ``rl_base_path`` and
        # ``rl_static_path`` are injected with the expected url_prefix.
        app = _FakeApp()
        runtime = _FakeRuntime()
        bp = self.web.register_racelink_web(app, runtime, url_prefix="/shared-ui")

        rendered = bp.routes[("/", ("GET",))]()
        kwargs = rendered["kwargs"]

        self.assertEqual(kwargs["rl_base_path"], "/shared-ui")
        self.assertEqual(kwargs["rl_static_path"], "/shared-ui/static")
        # The source is the literal SPA shell read from disk; sanity
        # check that the Jinja placeholders survive into the template
        # source (Flask's render path substitutes them server-side).
        self.assertIn("{{ rl_base_path }}", rendered["source"])


if __name__ == "__main__":
    unittest.main()
