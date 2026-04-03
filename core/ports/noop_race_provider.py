from __future__ import annotations

from .race_provider import RaceProviderPort


class NoOpRaceProvider(RaceProviderPort):
    """Neutral fallback provider that returns no active race data."""

    def get_current_heat(self) -> int | None:
        return None

    def get_pilot_assignments(self) -> list[tuple[int, str]]:
        return []

    def get_frequency_channels(self) -> list[str]:
        return []
