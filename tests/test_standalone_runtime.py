import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from tests._flask_stub import install_flask, install_serial


ROOT = pathlib.Path(__file__).resolve().parents[1]


class StandaloneRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name in (
            "flask",
            "racelink.integrations.standalone",
            "racelink.integrations.standalone.bootstrap",
            "racelink.integrations.standalone.config",
            "racelink.integrations.standalone.webapp",
        ):
            sys.modules.pop(name, None)
        install_serial()
        install_flask()
        from racelink.integrations.standalone import StandaloneConfig
        from racelink.integrations.standalone.bootstrap import build_standalone_runtime
        from racelink.integrations.standalone.webapp import create_standalone_app

        cls.StandaloneConfig = StandaloneConfig
        cls.build_standalone_runtime = staticmethod(build_standalone_runtime)
        cls.create_standalone_app = staticmethod(create_standalone_app)

    def _temp_path(self, filename: str) -> pathlib.Path:
        """Return a temp-dir path scoped to the current test.

        Using ``tempfile.TemporaryDirectory`` keeps artifacts outside the repo
        root so a mid-run crash cannot leave ``.standalone-test-*`` files
        behind for ``git status`` to surface.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return pathlib.Path(tmp.name) / filename

    def test_default_config_load_returns_expected_defaults(self):
        config_path = self._temp_path("standalone_config.json")
        try:
            config = self.StandaloneConfig.load(str(config_path))

            self.assertEqual(config.path, str(config_path))
            self.assertEqual(config.host, "127.0.0.1")
            self.assertEqual(config.port, 5077)
            self.assertFalse(config.debug)
            self.assertEqual(config.options, {})
        finally:
            if config_path.exists():
                config_path.unlink()

    def test_config_save_and_load_round_trip(self):
        config_path = self._temp_path("standalone_config.json")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            config = self.StandaloneConfig(
                path=str(config_path),
                host="0.0.0.0",
                port=5088,
                debug=True,
                options={"rl_comms_port": "COM7", "sample": "value"},
            )
            config.save()

            loaded = self.StandaloneConfig.load(str(config_path))

            self.assertEqual(loaded.host, "0.0.0.0")
            self.assertEqual(loaded.port, 5088)
            self.assertTrue(loaded.debug)
            self.assertEqual(loaded.options["rl_comms_port"], "COM7")
            self.assertEqual(loaded.options["sample"], "value")
        finally:
            if config_path.exists():
                config_path.unlink()

    def test_json_blob_option_stored_unescaped_on_disk(self):
        config_path = self._temp_path("standalone_config.json")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            blob = '{"schema_version": 2, "devices": [], "groups": []}'
            config = self.StandaloneConfig(
                path=str(config_path),
                options={"rl_state_v1": blob, "rl_comms_port": "COM7"},
            )
            config.save()

            # On disk the blob is a nested object (no double-escaped JSON),
            # while a plain-string option stays a plain string.
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertIsInstance(raw["options"]["rl_state_v1"], dict)
            self.assertEqual(raw["options"]["rl_state_v1"]["schema_version"], 2)
            self.assertEqual(raw["options"]["rl_comms_port"], "COM7")

            # On load the controller still receives the blob as a string.
            loaded = self.StandaloneConfig.load(str(config_path))
            self.assertIsInstance(loaded.options["rl_state_v1"], str)
            self.assertEqual(
                json.loads(loaded.options["rl_state_v1"])["schema_version"], 2,
            )
            self.assertEqual(loaded.options["rl_comms_port"], "COM7")
        finally:
            if config_path.exists():
                config_path.unlink()

    def test_create_standalone_app_returns_expected_runtime_shape(self):
        config_path = self._temp_path("standalone_config.json")
        try:
            config = self.StandaloneConfig(path=str(config_path))
            app, rl_app = self.create_standalone_app(config)

            self.assertEqual(getattr(app, "import_name", getattr(app, "name", None)), "racelink_standalone")
            self.assertIn("racelink", app.blueprints)
            self.assertIn(("/", ("GET",)), app.routes)
            self.assertIn("standalone", rl_app.integrations)
            self.assertIn("flask_app", rl_app.integrations)
            self.assertIs(rl_app.integrations["flask_app"], app)
            for service_name in ("config", "control", "gateway", "discovery", "status", "stream", "sync", "startblock", "ota", "presets", "host_wifi"):
                self.assertIn(service_name, rl_app.services)

            redirect = app.routes[("/", ("GET",))]()
            self.assertEqual(redirect, ("", 302, {"Location": "/racelink"}))
        finally:
            if config_path.exists():
                config_path.unlink()

    def test_build_standalone_runtime_exposes_default_source_and_sink(self):
        config_path = self._temp_path("standalone_config.json")
        try:
            runtime = self.build_standalone_runtime(config=str(config_path))

            self.assertEqual(runtime["config"].path, str(config_path))
            self.assertEqual(getattr(runtime["flask_app"], "import_name", getattr(runtime["flask_app"], "name", None)), "racelink_standalone")
            self.assertIs(runtime["race_link_app"].event_source, runtime["event_source"])
            self.assertIs(runtime["race_link_app"].data_sink, runtime["data_sink"])
        finally:
            if config_path.exists():
                config_path.unlink()


class StandaloneDocsTests(unittest.TestCase):
    # The two former tests in this class asserted against
    # ``docs/standalone.md`` and a "Consuming racelink-host from other
    # repositories" README section. Both targets were deliberately
    # moved to ``RaceLink_Docs/docs/RaceLink_Host/standalone-install.md``
    # during the 2026-04-30 documentation consolidation; the in-repo
    # docs directory was removed. The cross-repo doc layout is now
    # enforced by ``mkdocs build --strict`` in the docs repo. The
    # entrypoint contract (the bit that this Python package owns) is
    # still pinned by ``test_packaging_exposes_standalone_entrypoint``
    # below — that's the load-bearing assertion.

    def test_packaging_exposes_standalone_entrypoint(self):
        source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('racelink-standalone = "racelink.integrations.standalone.webapp:run_standalone"', source)

    def test_build_backend_ships_all_console_scripts(self):
        # The wheel's console scripts come from ``_build_backend.ENTRY_POINTS``,
        # NOT from pyproject's ``[project.scripts]`` (the custom PEP 517 backend
        # hardcodes them). The two must stay in sync or a script silently
        # vanishes from the wheel — which is how ``racelink-setup-nmcli`` went
        # missing once. Pin the build-relevant list here.
        from racelink import _build_backend

        scripts = _build_backend.ENTRY_POINTS["console_scripts"]
        names = {entry.split("=", 1)[0].strip() for entry in scripts}
        self.assertEqual(
            names,
            {"racelink-standalone", "racelink-host-version", "racelink-setup-nmcli"},
        )


class BrowserOpenHelperTests(unittest.TestCase):
    """The --open-browser / RACELINK_OPEN_BROWSER desktop-shortcut helpers."""

    @classmethod
    def setUpClass(cls):
        install_serial()
        install_flask()
        from racelink.integrations.standalone import webapp as webapp_module
        from racelink.integrations.standalone.config import StandaloneConfig
        cls.webapp = webapp_module
        cls.StandaloneConfig = StandaloneConfig

    def test_ui_url_uses_loopback_for_wildcard_bind(self):
        C = self.StandaloneConfig
        self.assertEqual(
            self.webapp._standalone_ui_url(C(host="127.0.0.1", port=5077)),
            "http://127.0.0.1:5077/racelink",
        )
        self.assertEqual(
            self.webapp._standalone_ui_url(C(host="0.0.0.0", port=5077)),
            "http://127.0.0.1:5077/racelink",
        )
        self.assertEqual(
            self.webapp._standalone_ui_url(C(host="192.168.1.5", port=8080)),
            "http://192.168.1.5:8080/racelink",
        )

    def test_browser_open_requested_via_flag_or_env(self):
        wm = self.webapp
        with mock.patch.object(sys, "argv", ["racelink-standalone"]), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RACELINK_OPEN_BROWSER", None)
            self.assertFalse(wm._browser_open_requested())
        with mock.patch.object(sys, "argv", ["racelink-standalone", "--open-browser"]):
            self.assertTrue(wm._browser_open_requested())
        with mock.patch.object(sys, "argv", ["racelink-standalone", "-o"]):
            self.assertTrue(wm._browser_open_requested())
        with mock.patch.object(sys, "argv", ["racelink-standalone"]), \
                mock.patch.dict(os.environ, {"RACELINK_OPEN_BROWSER": "1"}):
            self.assertTrue(wm._browser_open_requested())


if __name__ == "__main__":
    unittest.main()
