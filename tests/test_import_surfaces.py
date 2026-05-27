import importlib
import sys
import unittest

from tests._flask_stub import install_flask, install_serial


class ImportSurfaceTests(unittest.TestCase):
    def test_root_plugin_entrypoint_is_removed(self):
        self.assertNotIn("__init__", sys.modules)

    def test_canonical_package_imports_exist(self):
        install_serial()
        import racelink.domain  # noqa: F401
        import racelink.transport  # noqa: F401
        import racelink.web  # noqa: F401

    def test_controller_module_is_importable(self):
        install_serial()
        module = importlib.import_module("racelink.controller")

        self.assertTrue(hasattr(module, "RaceLink_Host"))

    def test_host_runtime_factory_is_importable(self):
        install_serial()
        module = importlib.import_module("racelink.app")

        self.assertTrue(callable(module.create_runtime))

    def test_standalone_surface_imports_without_rotorhazard_modules(self):
        install_serial()
        install_flask()
        module = importlib.import_module("racelink.integrations.standalone")

        self.assertTrue(callable(module.create_standalone_app))
        self.assertTrue(callable(module.build_standalone_runtime))


if __name__ == "__main__":
    unittest.main()
