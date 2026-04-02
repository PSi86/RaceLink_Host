from __future__ import annotations

from eventmanager import Evt


def activate(plugin) -> None:
    """Register import/export hooks for RaceLink config data."""
    controller = plugin.controller
    rhapi = plugin.rhapi

    rhapi.events.on(Evt.DATA_IMPORT_INITIALIZE, controller.host_ui.register_rl_dataimporter)
    rhapi.events.on(Evt.DATA_EXPORT_INITIALIZE, controller.host_ui.register_rl_dataexporter)
