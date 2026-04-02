from __future__ import annotations

import logging

from ....core.ports.runtime_ports import NotificationPort

logger = logging.getLogger(__name__)


class RHAPINotificationAdapter(NotificationPort):
    def __init__(self, rhapi):
        self._rhapi = rhapi

    def notify(self, message: str, level: str = "info") -> None:
        ui = getattr(self._rhapi, "ui", None)
        notify = getattr(ui, "notify", None)
        if callable(notify):
            notify(message, level)
            return
        message_notify = getattr(ui, "message_notify", None)
        if callable(message_notify):
            message_notify(message)
            return
        logger.info("[%s] %s", level.upper(), message)
