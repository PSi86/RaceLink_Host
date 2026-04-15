"""Created by Peter Simandl "PSi86" in 2026.
Works with Rotorhazard 4.0.
"""

def initialize(rhapi):
    from .racelink.integrations.rotorhazard import plugin as _rh_plugin

    return _rh_plugin.initialize(rhapi)


__all__ = [
    "initialize",
]
