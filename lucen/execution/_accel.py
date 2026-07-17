from __future__ import annotations

import os
from typing import Optional, Sequence

if os.environ.get("LUCEN_DISABLE_NATIVE"):
    _native = None
    ACCELERATED = False
else:
    try:
        from lucen import _core as _native  # type: ignore[attr-defined,no-redef]

        ACCELERATED = True
    except ImportError:  # pragma: no cover - env dependent
        _native = None
        ACCELERATED = False


def core_version() -> Optional[str]:
    return getattr(_native, "__version__", None) if _native else None


def audit_index_bitmap(chunk_index_lists: Sequence[Sequence[int]], length: int) -> Optional[int]:
    if _native is not None:
        return _native.audit_index_bitmap([list(indices) for indices in chunk_index_lists], length)
    seen: set = set()
    for indices in chunk_index_lists:
        local: set = set()
        for idx in indices:
            if idx in local:
                return idx
            local.add(idx)
        if not seen.isdisjoint(local):
            for idx in indices:
                if idx in seen:
                    return idx
        seen.update(local)
    return None


_FOLD_OPS = frozenset({"+", "*", "&", "|", "^", "min", "max"})

# distinct from None: a fold over user objects may legally produce None
UNHANDLED = object()


def fold_ordered(current, site_slabs, op, skip):
    if (
        _native is None
        or not isinstance(op, str)
        or op not in _FOLD_OPS
        or not site_slabs
        or not all(type(s) is list for s in site_slabs)
    ):
        return UNHANDLED
    return _native.fold_ordered(current, list(site_slabs), op, skip)


def audit_contiguous(ranges: Sequence["tuple[int, int]"], total: int) -> Optional[int]:
    if _native is not None:
        return _native.audit_contiguous(list(ranges), total)
    expected = 0
    for start, stop in sorted(ranges):
        if start != expected:
            return expected
        if stop < start or stop > total:
            return start
        expected = stop
    return None if expected == total else expected
