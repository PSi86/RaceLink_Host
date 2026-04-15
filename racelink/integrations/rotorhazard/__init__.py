"""RotorHazard integration package for RaceLink.

This package is intentionally kept at the repository edge so it can later move
to the dedicated plugin repository with minimal host-side changes.
"""

__all__ = [
    "initialize",
]


def __getattr__(name):
    if name == "initialize":
        from .plugin import initialize

        return initialize
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
