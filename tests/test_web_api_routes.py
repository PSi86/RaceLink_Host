import ast
import contextlib
import importlib
import pathlib
import sys
import types
import unittest

from racelink.domain import RL_Device, RL_DeviceGroup, RL_Dev_Type


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _ensure_flask_stub():
    if "flask" in sys.modules:
        return

    flask = types.ModuleType("flask")

    class Blueprint:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def route(self, *args, **kwargs):
            def _decorator(fn):
                return fn

            return _decorator

    flask.Blueprint = Blueprint
    flask.request = types.SimpleNamespace(get_json=lambda silent=True: {}, files={}, form={})
    flask.jsonify = lambda payload: payload
    flask.Response = type("Response", (), {})
    flask.stream_with_context = lambda fn: fn
    flask.templating = types.SimpleNamespace(render_template=lambda *args, **kwargs: {})
    sys.modules["flask"] = flask


def _import_api_module():
    _ensure_flask_stub()
    sys.modules.pop("racelink.web.api", None)
    return importlib.import_module("racelink.web.api")


class _FakeBlueprint:
    def __init__(self):
        self.routes = {}

    def route(self, rule, methods=None):
        def _decorator(fn):
            self.routes[(rule, tuple(methods or ("GET",)))] = fn
            return fn

        return _decorator


class _FakeContext:
    def __init__(self):
        self.rl_instance = type("RL", (), {"uiEffectList": [{"value": "01", "label": "Red"}]})()
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
        self.rl_lock = contextlib.nullcontext()
        self.RL_DeviceGroup = RL_DeviceGroup
        self.logger = None
        self.sse = type("SSE", (), {"master": type("Master", (), {"snapshot": staticmethod(lambda: {})})()})()
        self.tasks = type("Tasks", (), {"snapshot": staticmethod(lambda: {}), "is_running": staticmethod(lambda: False)})()
        self._devices = [
            RL_Device(
                "AABBCCDDEEFF",
                RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
                "SB",
                groupId=1,
                caps=RL_Dev_Type.NODE_WLED_STARTBLOCK_REV3,
            ),
            RL_Device(
                "112233445566",
                RL_Dev_Type.NODE_WLED_REV5,
                "WLED",
                groupId=2,
                caps=RL_Dev_Type.NODE_WLED_REV5,
            ),
        ]
        self._groups = [
            RL_DeviceGroup("Group 1"),
            RL_DeviceGroup("Group 2"),
            RL_DeviceGroup("All WLED Nodes", static_group=1, dev_type=0),
        ]

    def devices(self):
        return self._devices

    def groups(self):
        return self._groups


class WebApiRouteTests(unittest.TestCase):
    def setUp(self):
        self.api_module = _import_api_module()
        self.api_module.jsonify = lambda payload: payload
        self.bp = _FakeBlueprint()
        self.ctx = _FakeContext()
        self.api_module.register_api_routes(self.bp, self.ctx)

    def _route(self, path):
        return self.bp.routes[(path, ("GET",))]

    def test_specials_route_returns_specials_payload(self):
        payload = self._route("/racelink/api/specials")()

        self.assertTrue(payload["ok"])
        self.assertIn("specials", payload)
        self.assertIn("WLED", payload["specials"])
        self.assertIn("STARTBLOCK", payload["specials"])

    def test_neighboring_get_routes_execute_without_missing_symbols(self):
        devices = self._route("/racelink/api/devices")()
        groups = self._route("/racelink/api/groups")()
        options = self._route("/racelink/api/options")()

        self.assertTrue(devices["ok"])
        self.assertEqual(len(devices["devices"]), 2)
        self.assertTrue(groups["ok"])
        self.assertGreaterEqual(len(groups["groups"]), 1)
        self.assertTrue(options["ok"])
        self.assertEqual(options["effects"], [{"value": "01", "label": "Red"}])


class WebApiStaticGuardTests(unittest.TestCase):
    def test_web_api_has_no_free_get_specials_config_symbol(self):
        path = ROOT / "racelink" / "web" / "api.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        assigned = set()
        used = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.asname or alias.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                assigned.add(node.name)
                for arg in getattr(node, "args", ast.arguments()).args:
                    assigned.add(arg.arg)
            elif isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Store):
                    assigned.add(node.id)
                elif isinstance(node.ctx, ast.Load):
                    used.add(node.id)

        free_names = used - imported - assigned - set(dir(__builtins__))
        self.assertNotIn("get_specials_config", free_names)


if __name__ == "__main__":
    unittest.main()
