from __future__ import annotations

import array
import operator
from typing import Any, Callable, Dict, Hashable, List, Optional, Sequence, Union

from lucen.execution import _accel


class _Skip:
    _instance: "Optional[_Skip]" = None

    def __new__(cls) -> "_Skip":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # pickle must preserve SKIP identity across the process boundary
    def __reduce__(self):
        return (_Skip, ())

    def __repr__(self) -> str:
        return "<lucen.SKIP>"


SKIP = _Skip()


def new_list_slab(length: int) -> List[Any]:
    return [SKIP] * length


def commit_list_slab(container: Any, indices: Sequence[int], slab: Any) -> None:
    span = indices if isinstance(indices, range) and indices.step == 1 else None
    if span is not None and isinstance(slab, (array.array, bytearray)):
        try:
            container[span.start : span.stop] = slab
            return
        except (TypeError, ValueError):
            pass
    if type(container) is list and span is not None:
        try:
            slab.index(SKIP)
        except ValueError:
            container[span.start : span.stop] = slab
            return
    # measured: zip + list STORE_SUBSCR beats a native PyObject_SetItem loop
    for pos, value in zip(indices, slab):
        if value is not SKIP:
            container[pos] = value


def commit_dict_slab(container: Any, slab: Dict) -> None:
    if isinstance(container, dict):
        container.update(slab)
        return
    for key, value in slab.items():
        container[key] = value


def audit_disjoint_dict_slabs(
    slabs: Sequence[Dict], index_bound: Optional[int] = None
) -> Optional[Hashable]:
    if index_bound is not None and _accel.ACCELERATED:
        key_lists = [list(slab.keys()) for slab in slabs]
        if all(type(k) is int and 0 <= k < index_bound for keys in key_lists for k in keys):
            return _accel.audit_index_bitmap(key_lists, index_bound)
    seen: set = set()
    for slab in slabs:
        keys = slab.keys()
        if not seen.isdisjoint(keys):
            for key in keys:
                if key in seen:
                    return key
        seen.update(keys)
    return None


_FOLD: Dict[str, Callable[[Any, Any], Any]] = {
    "+": operator.add,
    "*": operator.mul,
    "min": min,
    "max": max,
    "&": operator.and_,
    "|": operator.or_,
    "^": operator.xor,
}


def fold_contributions(
    current: Any, site_slabs: Sequence[List[Any]], op: Union[str, Callable[[Any, Any], Any]]
) -> Any:
    # same number protocol in the same order as the loop below, just without
    # per-element bytecode dispatch
    folded = _accel.fold_ordered(current, site_slabs, op, SKIP)
    if folded is not _accel.UNHANDLED:
        return folded
    combine = _FOLD[op] if isinstance(op, str) else op
    length = len(site_slabs[0]) if site_slabs else 0
    for j in range(length):
        for slab in site_slabs:
            value = slab[j]
            if value is not SKIP:
                current = combine(current, value)
    return current


def resolve_path(env: Dict[str, Any], path: str) -> Any:
    obj = env[path.split(".", 1)[0]]
    for part in path.split(".")[1:]:
        obj = getattr(obj, part)
    return obj


def assign_path(env: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    if len(parts) == 1:
        env[parts[0]] = value
        return
    obj = env[parts[0]]
    for part in parts[1:-1]:
        obj = getattr(obj, part)
    setattr(obj, parts[-1], value)
