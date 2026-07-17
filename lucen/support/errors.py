from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Set, Tuple, Union

_log = logging.getLogger("lucen")


class ErrorsMode(Enum):
    """How a fallback is surfaced: REPORT (default, recorded quietly), QUIET
    (recorded, no report), or HARD (raised)."""

    REPORT = "report"
    QUIET = "quiet"
    HARD = "hard"


_mode = ErrorsMode.REPORT


def set_errors_mode(mode: Union[ErrorsMode, str]) -> None:
    """Set the process-wide fallback mode, by ErrorsMode or its string name."""
    global _mode
    _mode = mode if isinstance(mode, ErrorsMode) else ErrorsMode(mode)


def get_errors_mode() -> ErrorsMode:
    """Return the current process-wide fallback mode."""
    return _mode


class LucenError(Exception):
    """Base class for every Lucen error; carries the file and line of the
    marked block it concerns."""

    def __init__(self, message: str, *, file: Optional[str] = None, line: Optional[int] = None):
        self.message = message
        self.file = file
        self.line = line
        loc = f"{file}:{line}: " if file is not None and line is not None else ""
        super().__init__(loc + message)


class ClauseValueError(LucenError):
    pass


class PragmaSyntaxError(LucenError):
    pass


class PragmaStructureError(LucenError):
    pass


class PragmaScopeError(LucenError):
    pass


class TrustPragmaScopeError(LucenError):
    pass


class UnsupportedIterableError(LucenError):
    pass


class IllegalSyntaxInBlockError(LucenError):
    pass


class BranchMergeConflictError(LucenError):
    pass


class DependencyCycleError(LucenError):
    pass


class UnresolvedDependencyShapeError(LucenError):
    pass


class UnmergeableConflictError(LucenError):
    pass


class MonotonicDependencyError(LucenError):
    pass


class UnprofitableParallelismError(LucenError):
    pass


class AmbiguousTrustedNameError(LucenError):
    pass


class PreflightCheckError(LucenError):
    pass


class ParallelWriteConflictError(LucenError):
    pass


class MidRunSerializationError(LucenError):
    pass


class ParallelTimeoutError(LucenError):
    pass


class NestedParallelRegionError(LucenError):
    pass


@dataclass(frozen=True)
class FallbackRecord:
    file: Optional[str]
    line: Optional[int]
    error: str
    message: str


_records: List[FallbackRecord] = []
_logged_once: Set[Tuple[Optional[str], Optional[int], str]] = set()


def raise_or_fallback(exc: LucenError) -> FallbackRecord:
    if _mode is ErrorsMode.HARD:
        raise exc
    rec = FallbackRecord(exc.file, exc.line, type(exc).__name__, exc.message)
    _records.append(rec)
    if _mode is ErrorsMode.REPORT:
        _log_once(rec)
    return rec


def report_fallback(
    reason: str, *, file: Optional[str] = None, line: Optional[int] = None, error: str = "Fallback"
) -> FallbackRecord:
    rec = FallbackRecord(file, line, error, reason)
    _records.append(rec)
    if _mode is ErrorsMode.REPORT:
        _log_once(rec)
    return rec


def get_fallback_report() -> Tuple[FallbackRecord, ...]:
    """Every fallback recorded so far: one FallbackRecord per block that ran
    sequentially or downgraded, each with its error, file, line, and reason."""
    return tuple(_records)


def clear_fallback_report() -> None:
    """Discard all recorded fallback records."""
    _records.clear()
    _logged_once.clear()


def _log_once(rec: FallbackRecord) -> None:
    key = (rec.file, rec.line, rec.error)
    if key in _logged_once:
        return
    _logged_once.add(key)
    _log.warning("lucen fallback: %s (%s:%s): %s", rec.error, rec.file, rec.line, rec.message)
