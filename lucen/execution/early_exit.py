from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from lucen.execution import nested_guard
from lucen.execution.runtime import commit_list_slab, resolve_path


def execute_early_exit(
    spec,
    plan,
    env: Dict[str, Any],
    module_globals: Optional[Dict[str, Any]],
    gate,
    workers: int,
    backend: str,
    stats,
) -> Optional[tuple]:
    from lucen.execution import dispatch

    stats["backend"] = "thread"
    _, n_chunks = dispatch._sizing(spec, plan.n, "thread")
    bounds = dispatch._bounds(plan.n, n_chunks)
    chunk_fn, _ = spec.fns()
    pool = dispatch._ensure_pool(max(workers, _cpu()))
    budget = threading.Semaphore(workers)

    lowest = _LowWater()
    records: List[dispatch._Record] = []
    records_lock = threading.Lock()

    def job(idx: int, a: int, b: int) -> None:
        if lowest.value() <= a:
            return
        record = dispatch._new_record(spec, plan, idx, a, b)
        exit_holder: List[Any] = [None]
        args = _chunk_args(spec, plan, record, env, module_globals, gate, exit_holder)
        with budget, nested_guard.dispatch_scope():
            chunk_fn(*args)
        if exit_holder[0] is not None:
            record.exit_pos = exit_holder[0]
            lowest.offer(exit_holder[0])
        with records_lock:
            records.append(record)

    from concurrent.futures import wait

    futures = [pool.submit(job, idx, a, b) for idx, (a, b) in enumerate(bounds, start=1)]
    done, _ = wait(futures)
    for fut in done:
        exc = fut.exception()
        if exc is not None:
            raise exc

    exit_pos = lowest.value()
    records.sort(key=lambda r: r.idx)
    for record in records:
        if record.a > exit_pos:
            continue
        _commit_prefix(spec, plan, record, env, exit_pos)

    stats["parallel_runs"] += 1
    stats["chunks"] += len(records)
    return _rebind_exit(spec, plan, env, exit_pos)


class _LowWater:
    def __init__(self) -> None:
        self._v = float("inf")
        self._lock = threading.Lock()

    def offer(self, pos: int) -> None:
        with self._lock:
            if pos < self._v:
                self._v = pos

    def value(self):
        with self._lock:
            return self._v


def _commit_prefix(spec, plan, record, env, exit_pos) -> None:
    limit = min(record.b, exit_pos + 1)
    width = limit - record.a
    if width <= 0:
        return
    for slab_plan in spec.artifact.slabs:
        container = resolve_path(env, slab_plan.container)
        slab = record.slabs[slab_plan.param]
        if slab_plan.kind == "list":
            commit_list_slab(container, plan.indices(record.a, limit), slab[:width])
        else:
            for key, value in list(slab.items())[:width]:
                container[key] = value


def _rebind_exit(spec, plan, env, exit_pos) -> Optional[tuple]:
    artifact = spec.artifact
    pos = min(exit_pos, plan.n - 1) if exit_pos != float("inf") else plan.n - 1
    element = _element_at(plan, pos)
    ns: Dict[str, Any] = {"_v": element}
    exec(f"{artifact.target_source} = _v", {}, ns)
    return tuple(ns[t] for t in artifact.loop_targets)


def _element_at(plan, pos: int):
    if plan.domain == "range":
        return plan.base[pos]
    if plan.domain == "enumerate":
        return (pos, plan.seq[pos])
    return plan.seq[pos]


def _chunk_args(spec, plan, record, env, module_globals, gate, exit_holder):
    from lucen.execution import dispatch

    args: List[Any] = []
    for p in spec.artifact.params:
        if p == "_plx_exit":
            args.append(exit_holder)
        else:
            args.append(dispatch._one_arg(p, spec, plan, record, env, module_globals, gate, None))
    return args


def _cpu() -> int:
    import os

    return os.cpu_count() or 4
