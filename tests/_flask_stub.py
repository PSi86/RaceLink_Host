"""Shared Flask + ``serial`` stubs for tests that import the racelink web
layer without pulling in the real dependencies.

Tests that need to drive imports of ``racelink.web.*`` can call
``install_flask()`` (and ``install_serial()`` for the controller / runtime
modules) at top level; both are idempotent and augment any existing
``sys.modules`` entry rather than replacing it.

The smoke test in ``test_installed_artifact_smoke.py`` cannot use this
helper — its body runs inside a fresh virtualenv subprocess that only
has the installed wheel on its ``sys.path``. That stub stays inline.
"""

from __future__ import annotations

import sys
import types


def install_serial() -> None:
    """Install lightweight ``serial`` + ``serial.tools.list_ports`` stubs.

    Idempotent — returns early if ``serial`` is already on ``sys.modules``.
    """
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


def install_flask() -> types.ModuleType:
    """Install a stub ``flask`` module on ``sys.modules`` and return it.

    Provides the surface the racelink web layer touches at import time:
    ``Flask`` / ``Blueprint`` classes (with route-recording semantics),
    ``request``, ``jsonify``, ``Response``, ``stream_with_context``, and
    a ``templating`` namespace covering both ``render_template`` and
    ``render_template_string``.

    Tests that need to assert on a specific call-site shape (e.g.
    ``test_web_api_routes.py`` overrides ``flask.jsonify`` to ``lambda
    payload: payload``) replace the relevant attribute after calling
    this function.
    """
    flask = sys.modules.get("flask")
    if flask is None:
        flask = types.ModuleType("flask")
        sys.modules["flask"] = flask

    class Flask:
        def __init__(self, name=None, *args, **kwargs):
            self.name = name
            self.import_name = name
            self.args = args
            self.kwargs = kwargs
            self.blueprints = {}
            self.routes = {}

        def register_blueprint(self, blueprint):
            self.blueprints[blueprint.name] = blueprint

        def route(self, rule, methods=None):
            def _decorator(fn):
                self.routes[(rule, tuple(methods or ("GET",)))] = fn
                return fn
            return _decorator

        def run(self, *args, **kwargs):
            return None

    class Blueprint:
        def __init__(self, name=None, import_name=None, *args, **kwargs):
            self.name = name
            self.import_name = import_name
            self.args = args
            self.kwargs = kwargs
            self.routes = {}

        def route(self, rule, methods=None):
            def _decorator(fn):
                self.routes[(rule, tuple(methods or ("GET",)))] = fn
                return fn
            return _decorator

    flask.Flask = Flask
    flask.Blueprint = Blueprint
    flask.templating = types.SimpleNamespace(
        render_template=lambda *args, **kwargs: {"args": args, "kwargs": kwargs},
        render_template_string=lambda source, **kwargs: {"source": source, "kwargs": kwargs},
    )
    flask.request = types.SimpleNamespace(
        args={}, json=None, form={}, files={},
        get_json=lambda silent=True: {},
    )
    flask.jsonify = lambda *args, **kwargs: {"args": args, "kwargs": kwargs}
    flask.Response = type("Response", (), {})
    flask.stream_with_context = lambda fn: fn

    return flask
