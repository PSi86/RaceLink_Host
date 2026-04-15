"""RotorHazard plugin bootstrap for RaceLink.

RL-002 moves the RotorHazard-specific initialization flow out of the root
plugin module while keeping the existing runtime behavior unchanged.
"""

import logging

from eventmanager import Evt

from ...app import create_runtime
from ...core import NullSink
from ...domain import RL_DeviceGroup
from ...state import get_runtime_state_repository
from ...web import register_rl_blueprint
from .ui import RotorHazardUIAdapter
from controller import RaceLink_Host

logger = logging.getLogger(__name__)

rl_app = None
rl_instance = None


def initialize(rhapi):
    global rl_app, rl_instance

    state_repository = get_runtime_state_repository()

    # Keep RotorHazard wiring local to this package so it can move out to the
    # plugin repository without dragging broader host internals with it.
    controller = RaceLink_Host(
        rhapi,
        "RaceLink_Host",
        "RaceLink",
        state_repository=state_repository,
    )
    rh_adapter = RotorHazardUIAdapter(controller, rhapi)
    controller.rh_adapter = rh_adapter
    controller.rh_source = rh_adapter.source
    rl_app = create_runtime(
        rhapi,
        state_repository=state_repository,
        controller=controller,
        presets_apply_options=rh_adapter.apply_presets_options,
        integrations={"rotorhazard": rhapi, "rotorhazard_ui": rh_adapter, "rotorhazard_source": rh_adapter.source},
        event_source=rh_adapter.source,
        data_sink=NullSink(),
    )
    rl_instance = rl_app.rl_instance

    register_rl_blueprint(
        rhapi,
        rl_instance=rl_app.rl_instance,
        state_repository=state_repository,
        services=rl_app.services,
        RL_DeviceGroup=RL_DeviceGroup,
        logger=logger,
    )

    rhapi.events.on(Evt.DATA_IMPORT_INITIALIZE, rh_adapter.register_rl_dataimporter)
    rhapi.events.on(Evt.DATA_EXPORT_INITIALIZE, rh_adapter.register_rl_dataexporter)
    rhapi.events.on(Evt.ACTIONS_INITIALIZE, rh_adapter.registerActions)

    rhapi.events.on(Evt.STARTUP, rl_app.rl_instance.onStartup)

    rhapi.events.on(Evt.RACE_START, rl_app.rl_instance.onRaceStart)
    rhapi.events.on(Evt.RACE_FINISH, rl_app.rl_instance.onRaceFinish)
    rhapi.events.on(Evt.RACE_STOP, rl_app.rl_instance.onRaceStop)
