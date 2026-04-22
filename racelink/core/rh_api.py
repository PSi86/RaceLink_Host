"""Typed contract for the RotorHazard host API consumed by RaceLink (plan P2-1).

The RaceLink host never reaches into RotorHazard's internals directly; it only
talks to a small surface of callables exposed on the ``rhapi`` object. This
module defines that surface as a set of ``typing.Protocol``s so tools like
mypy/pyright can flag accidental drift, and so the standalone host shim has an
authoritative spec to match.

Runtime code keeps duck-typing (``getattr``/``hasattr`` checks) because older
RotorHazard builds may be missing optional surfaces (``ui``, ``events``).
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, runtime_checkable


@runtime_checkable
class RHDbApi(Protocol):
    def option(self, key: str, default: Any = None) -> Any: ...
    def option_set(self, key: str, value: Any) -> None: ...


@runtime_checkable
class RHUiApi(Protocol):
    def message_notify(self, message: str) -> None: ...
    def broadcast_ui(self, panel: str) -> None: ...


@runtime_checkable
class RHEventsApi(Protocol):
    def on(self, event: Any, handler: Callable[..., Any]) -> Any: ...


@runtime_checkable
class RHApi(Protocol):
    """Minimum surface the RaceLink host expects from a RotorHazard-style API.

    ``ui`` and ``events`` are typed as ``Optional`` because the standalone shim
    does not implement notifications or event dispatch.
    """

    db: RHDbApi
    ui: Optional[RHUiApi]
    events: Optional[RHEventsApi]

    def __call__(self, text: str) -> str:
        """Translator hook invoked as ``rhapi.__(text)``."""
        ...
