import importlib
import sys
import types
import unittest


def _ensure_serial_stub():
    if "serial" in sys.modules:
        return
    serial_stub = types.ModuleType("serial")
    serial_stub.Serial = object
    serial_stub.SerialException = Exception
    sys.modules["serial"] = serial_stub

    serial_tools = types.ModuleType("serial.tools")
    serial_list_ports = types.ModuleType("serial.tools.list_ports")
    serial_list_ports.comports = lambda: []
    serial_tools.list_ports = serial_list_ports
    serial_stub.tools = serial_tools
    sys.modules["serial.tools"] = serial_tools
    sys.modules["serial.tools.list_ports"] = serial_list_ports


def _ensure_flask_stub():
    flask = sys.modules.get("flask")
    if flask is None:
        flask = types.ModuleType("flask")
        sys.modules["flask"] = flask

    class Flask:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.blueprints = {}

        def register_blueprint(self, blueprint):
            self.blueprints[blueprint.name] = blueprint

        def route(self, *_args, **_kwargs):
            def _decorator(fn):
                return fn

            return _decorator

        def run(self, *args, **kwargs):
            return None

    class Blueprint:
        def __init__(self, name, *args, **kwargs):
            self.name = name
            self.args = args
            self.kwargs = kwargs

        def route(self, *args, **kwargs):
            def _decorator(fn):
                return fn

            return _decorator

    flask.Flask = Flask
    flask.Blueprint = Blueprint
    flask.templating = types.SimpleNamespace(render_template=lambda *args, **kwargs: {})
    flask.request = types.SimpleNamespace(args={}, json=None, form={}, get_json=lambda silent=True: {})
    flask.jsonify = lambda *args, **kwargs: {"args": args, "kwargs": kwargs}
    flask.Response = type("Response", (), {})
    flask.stream_with_context = lambda fn: fn


class ImportSurfaceTests(unittest.TestCase):
    def test_root_plugin_entrypoint_is_removed(self):
        self.assertNotIn("__init__", sys.modules)

    def test_canonical_package_imports_exist(self):
        _ensure_serial_stub()
        import racelink.domain  # noqa: F401
        import racelink.transport  # noqa: F401
        import racelink.web  # noqa: F401

    def test_top_level_controller_module_is_importable(self):
        _ensure_serial_stub()
        module = importlib.import_module("controller")

        self.assertTrue(hasattr(module, "RaceLink_Host"))

    def test_host_runtime_factory_is_importable(self):
        _ensure_serial_stub()
        module = importlib.import_module("racelink.app")

        self.assertTrue(callable(module.create_runtime))

    def test_standalone_surface_imports_without_rotorhazard_modules(self):
        _ensure_serial_stub()
        _ensure_flask_stub()
        module = importlib.import_module("racelink.integrations.standalone")

        self.assertTrue(callable(module.create_standalone_app))
        self.assertTrue(callable(module.build_standalone_runtime))


if __name__ == "__main__":
    unittest.main()
