"""Business services for RaceLink application behavior."""

import sys

from .config_service import ConfigService
from .control_service import ControlService
from .discovery_service import DiscoveryService
from .gateway_service import GatewayService
from .host_wifi_service import HostWifiService
from .onboarding_service import OnboardingService
from .ota_service import OTAService
from .ota_workflow_service import OTAWorkflowService
from .presets_service import PresetsService
from .rl_presets_service import RLPresetsService
from .scene_runner_service import SceneRunnerService
from .scenes_service import SceneService
from .specials_service import SpecialsService
from .startblock_service import StartblockService, build_startblock_payload_v1
from .status_service import StatusService
from .stream_service import StreamService
from .sync_service import SyncService


def create_host_wifi_service():
    """Return the host-WiFi backend for the current OS.

    Windows uses the ``netsh wlan`` backend; everything else uses the
    nmcli/NetworkManager backend. Both expose the same public API the OTA
    workflow relies on.
    """
    if sys.platform.startswith("win"):
        from .netsh_wifi_service import NetshWifiService
        return NetshWifiService()
    return HostWifiService()


__all__ = [
    "ConfigService",
    "ControlService",
    "create_host_wifi_service",
    "DiscoveryService",
    "GatewayService",
    "HostWifiService",
    "OnboardingService",
    "OTAService",
    "OTAWorkflowService",
    "PresetsService",
    "RLPresetsService",
    "SceneRunnerService",
    "SceneService",
    "SpecialsService",
    "StartblockService",
    "StatusService",
    "StreamService",
    "SyncService",
    "build_startblock_payload_v1",
]
