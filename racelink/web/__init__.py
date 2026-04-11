"""Web-layer package for API, SSE, DTOs, and task orchestration."""

__all__ = ["register_rl_blueprint"]


def __getattr__(name):
    if name == "register_rl_blueprint":
        from .blueprint import register_rl_blueprint

        return register_rl_blueprint
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
