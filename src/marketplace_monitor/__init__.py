"""Local Marketplace Monitor.

A personal automation that scans local for-sale listings across multiple
marketplaces once per day, scores each listing against a natural-language
description of what you're looking for, and emails a ranked digest of the best
matches. See the design doc / README for the full picture.
"""

__version__ = "1.0.0"

from .config import load_config

__all__ = ["load_config", "run", "__version__"]


def __getattr__(name):
    # Lazily expose `run` so `python -m marketplace_monitor.run` doesn't import
    # the submodule twice (avoids a RuntimeWarning), while `from marketplace_monitor
    # import run` still works.
    if name == "run":
        from .run import run

        return run
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
