from __future__ import annotations

import logging

from eventmanager import Evt

from ..controller import RaceLink_LoRa
from ..core.repository import InMemoryDeviceRepository
from ..data import RL_DeviceGroup
from ..integrations.rotorhazard.adapters import (
    RHAPINotificationAdapter,
    RHAPIRaceHostAdapter,
    RHAPIUiExtensionAdapter,
    RHAPIRaceProviderAdapter,
)
from ..racelink_webui import register_rl_blueprint

logger = logging.getLogger(__name__)


class RotorHazardComposition:
    def __init__(self, rhapi):
        self.rhapi = rhapi
        self.repository = InMemoryDeviceRepository()
        self.race_host = RHAPIRaceHostAdapter(rhapi)
        self.notifier = RHAPINotificationAdapter(rhapi)
        self.ui_extension = RHAPIUiExtensionAdapter(rhapi)
        self.race_provider = RHAPIRaceProviderAdapter(self.race_host)
        self.rl_instance: RaceLink_LoRa | None = None

    def initialize(self) -> RaceLink_LoRa:
        self.rl_instance = RaceLink_LoRa(
            race_host=self.race_host,
            notifier=self.notifier,
            ui_extension=self.ui_extension,
            name="RaceLink_LoRa",
            label="RaceLink",
            repository=self.repository,
            race_provider=self.race_provider,
        )

        register_rl_blueprint(
            self.rhapi,
            rl_instance=self.rl_instance,
            rl_devicelist=self.repository.device_items,
            rl_grouplist=self.repository.group_items,
            RL_DeviceGroup=RL_DeviceGroup,
            logger=logger,
        )

        self.race_host.on(Evt.DATA_IMPORT_INITIALIZE, self.rl_instance.register_rl_dataimporter)
        self.race_host.on(Evt.DATA_EXPORT_INITIALIZE, self.rl_instance.register_rl_dataexporter)
        self.race_host.on(Evt.ACTIONS_INITIALIZE, self.rl_instance.registerActions)
        self.race_host.on(Evt.STARTUP, self.rl_instance.onStartup)
        self.race_provider.on_race_start(self.rl_instance.onRaceStart)
        self.race_provider.on_race_finish(self.rl_instance.onRaceFinish)
        self.race_provider.on_race_stop(self.rl_instance.onRaceStop)
        return self.rl_instance


def compose_rotorhazard(rhapi) -> RotorHazardComposition:
    return RotorHazardComposition(rhapi)
