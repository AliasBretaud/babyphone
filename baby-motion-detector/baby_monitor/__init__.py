"""
Baby monitor analyzer package.

Provides WebRTC client components to consume or produce the BabyPhone stream.
"""

from importlib import import_module
from typing import Any

__all__ = (
    "AnalyzerClient",
    "AnalyzerConfig",
    "BroadcasterConfig",
    "HeadlessBroadcaster",
)


def __getattr__(name: str) -> Any:
    if name == "AnalyzerClient":
        module = import_module(".analyzer", __name__)
    elif name == "AnalyzerConfig":
        module = import_module(".config", __name__)
    elif name in {"BroadcasterConfig", "HeadlessBroadcaster"}:
        module = import_module(".broadcaster", __name__)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    value = getattr(module, name)
    globals()[name] = value
    return value
