from __future__ import annotations

from typing import Any, Callable

from ....core.ports.runtime_ports import UiExtensionPort


class RHAPIUiExtensionAdapter(UiExtensionPort):
    def __init__(self, rhapi):
        self._rhapi = rhapi

    def broadcast_ui(self, panel: str) -> None:
        self._rhapi.ui.broadcast_ui(panel)

    def register_panel(self, panel_id: str, title: str, location: str) -> None:
        self._rhapi.ui.register_panel(panel_id, title, location)

    def register_option(self, option: Any, panel_id: str) -> None:
        self._rhapi.fields.register_option(option, panel_id)

    def register_quickbutton(self, panel_id: str, button_id: str, label: str, handler: Callable[..., Any], args: dict | None = None) -> None:
        self._rhapi.ui.register_quickbutton(panel_id, button_id, label, handler, args=args)

    def blueprint_add(self, blueprint: Any) -> None:
        self._rhapi.ui.blueprint_add(blueprint)
