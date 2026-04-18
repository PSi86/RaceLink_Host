"""Web-layer package for shared RaceLink UI registration and HTTP/SSE support."""

__all__ = [
    "RaceLinkWebRuntime",
    "create_racelink_web_blueprint",
    "register_racelink_web",
    "register_rl_blueprint",
]


def __getattr__(name):
    if name in {
        "RaceLinkWebRuntime",
        "create_racelink_web_blueprint",
        "register_racelink_web",
        "register_rl_blueprint",
    }:
        from .blueprint import (
            RaceLinkWebRuntime,
            create_racelink_web_blueprint,
            register_racelink_web,
            register_rl_blueprint,
        )

        return {
            "RaceLinkWebRuntime": RaceLinkWebRuntime,
            "create_racelink_web_blueprint": create_racelink_web_blueprint,
            "register_racelink_web": register_racelink_web,
            "register_rl_blueprint": register_rl_blueprint,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
