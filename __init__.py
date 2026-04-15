"""Compatibility shim for the legacy RotorHazard plugin entrypoint.

The actual RotorHazard integration lives under
``racelink.integrations.rotorhazard``. This root module remains only so the
current RH plugin loader can import ``initialize`` without knowing host package
internals.
"""

def initialize(rhapi):
    from .racelink.integrations.rotorhazard import plugin as _rh_plugin

    return _rh_plugin.initialize(rhapi)


__all__ = [
    "initialize",
]
