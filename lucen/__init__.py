from __future__ import annotations

import os
import sys
from dataclasses import replace
from typing import Iterable

from lucen.support.errors import (
    ClauseValueError,
    ErrorsMode,
    ParallelTimeoutError,
    ParallelWriteConflictError,
    LucenError,
    clear_fallback_report,
    get_errors_mode,
    get_fallback_report,
    set_errors_mode,
)

__version__ = "1.0.0"

__all__ = [
    "activate",
    "deactivate",
    "get_collected_errors",
    "get_fallback_report",
    "clear_fallback_report",
    "get_errors_mode",
    "set_errors_mode",
    "ErrorsMode",
    "LucenError",
    "ClauseValueError",
    "ParallelTimeoutError",
    "ParallelWriteConflictError",
    "__version__",
]

_KNOWN_EXPERIMENTAL: frozenset = frozenset({"early_exit", "branch_sensitive_deps", "typed_buffers"})
_AVAILABLE_EXPERIMENTAL: frozenset = _KNOWN_EXPERIMENTAL


def activate(experimental: Iterable[str] = ()) -> None:
    """Install the import hook. Idempotent."""
    from lucen import import_hook
    from lucen.support import config

    requested = set(experimental)
    unknown = requested - _KNOWN_EXPERIMENTAL
    if unknown:
        raise ValueError(f"unknown experimental feature(s): {sorted(unknown)}")
    unavailable = requested - _AVAILABLE_EXPERIMENTAL
    if unavailable:
        raise ValueError(
            f"experimental feature(s) {sorted(unavailable)} are not available "
            "in this release (spec 12); blocks depending on them run "
            "sequentially by default"
        )

    cfg = config.active()
    path = config.discover()
    if path is not None:
        cfg = config.load(path)
    effective = (
        frozenset()
        if not cfg.allow_experimental
        else (cfg.experimental | requested) & _AVAILABLE_EXPERIMENTAL
    )
    config.set_active(replace(cfg, experimental=effective))
    entry = (
        os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else os.getcwd()
    )
    import_hook.install(entry)


def deactivate() -> None:
    """Uninstall the hook; already-imported modules keep their rewrites."""
    from lucen import import_hook

    import_hook.uninstall()


def get_collected_errors(key=None):
    """Per-iteration exceptions gathered by blocks running under on_error=collect.

    With no key, returns a mapping of block key to its collected errors; with a
    (filename, line) key, returns that block's list."""
    from lucen.execution import dispatch

    return dispatch.get_collected_errors(key)
