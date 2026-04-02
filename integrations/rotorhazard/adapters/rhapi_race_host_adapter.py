from __future__ import annotations

from typing import Any, Callable

from ....core.ports.runtime_ports import RaceHostPort


class RHAPIRaceHostAdapter(RaceHostPort):
    def __init__(self, rhapi):
        self._rhapi = rhapi

    def option(self, key: str, default: Any = None) -> Any:
        return self._rhapi.db.option(key, default)

    def option_set(self, key: str, value: Any) -> None:
        self._rhapi.db.option_set(key, value)

    def on(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._rhapi.events.on(event_name, handler)

    def trigger(self, event_name: str, payload: Any = None) -> None:
        trigger = getattr(self._rhapi.events, "trigger", None)
        if callable(trigger):
            trigger(event_name, payload)

    def translate(self, text: str) -> str:
        fn = getattr(self._rhapi, "__", None)
        if callable(fn):
            return fn(text)
        return text

    @property
    def race(self):
        return getattr(self._rhapi, "race", None)

    @property
    def racecontext(self):
        return getattr(self._rhapi, "_racecontext", None)
