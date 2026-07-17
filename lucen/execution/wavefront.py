from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from lucen.execution import dispatch, preflight
from lucen.execution.runtime import audit_disjoint_dict_slabs, resolve_path
from lucen.support.errors import ParallelWriteConflictError, report_fallback


def execute_wavefront(
    spec,
    plan,
    env: Dict[str, Any],
    module_globals: Optional[Dict[str, Any]],
    gate,
    workers: int,
    backend: str,
    stats,
) -> Optional[tuple]:
    if spec.dag_divisor is not None:
        levels = _divisor_levels(spec, plan)
        if levels is None:
            report_fallback(
                "wavefront needs an ascending iteration order; ran SEQUENTIAL",
                file=spec.filename,
                line=spec.line,
                error="WavefrontOrder",
            )
            return dispatch._run_twin(spec, plan, env, module_globals, stats)
    else:
        levels = _key_levels(spec, plan, env, module_globals)
        if levels is None:
            report_fallback(
                "depend=acyclic order key is not resolvable; ran SEQUENTIAL",
                file=spec.filename,
                line=spec.line,
                error="WavefrontOrder",
            )
            return dispatch._run_twin(spec, plan, env, module_globals, stats)

    stats["backend"] = backend
    deadline = dispatch._deadline(spec)
    all_records: list = []
    for a, b in levels:
        width = b - a
        if width < spec.grainsize:
            record = dispatch._new_record(spec, plan, 0, a, b)
            args = dispatch._chunk_args(spec, plan, record, env, module_globals, gate, deadline)
            chunk_fn, _ = spec.fns()
            try:
                chunk_fn(*args)
            except BaseException:
                dispatch._commit_records(spec, plan, [record], env, module_globals, gate)
                raise
            records, first_error = [record], None
        else:
            sub = dispatch._bounds(width, min(4 * workers, width))
            sub = [(a + x, a + y) for x, y in sub]
            records, first_error = dispatch._run_chunk_set(
                spec, plan, sub, env, module_globals, gate, workers, backend, deadline, stats
            )
            records.sort(key=lambda r: r.idx)
            if first_error is not None:
                prefix = [r for r in records if r.idx <= first_error[0]]
                dispatch._commit_records(spec, plan, prefix, env, module_globals, gate)
                raise first_error[1]
        if not spec.skip_runtime_check:
            for slab_plan in spec.artifact.slabs:
                if slab_plan.kind != "dict":
                    continue
                container = resolve_path(env, slab_plan.container)
                bound = len(container) if isinstance(container, list) else None
                overlap = audit_disjoint_dict_slabs(
                    [r.slabs[slab_plan.param] for r in records], index_bound=bound
                )
                if overlap is not None:
                    raise ParallelWriteConflictError(
                        f"depend=acyclic assertion violated: "
                        f"'{slab_plan.container}[{overlap!r}]' written twice; "
                        "earlier buckets are already committed, so this cannot "
                        "be transparently re-run",
                        file=spec.filename,
                        line=spec.line,
                    )
        dispatch._commit_records(spec, plan, records, env, module_globals, gate)
        stats["chunks"] += len(records)
        completed = all_records[-1].b if all_records else 0
        dispatch._emit_progress(spec, gate, records, plan.n, base_completed=completed)
        all_records.extend(records)
    stats["parallel_runs"] += 1
    dispatch._flush_collected(spec, all_records, gate)
    return dispatch._rebind(spec, plan, env, module_globals)


def _divisor_levels(spec, plan) -> Optional[List[Tuple[int, int]]]:
    if plan.domain == "range":
        start, step = plan.base.start, plan.base.step
        if step <= 0:
            return None
    else:
        start, step = 0, 1
    c = spec.dag_divisor
    n = plan.n
    levels: List[Tuple[int, int]] = []
    prev = 0
    threshold = 1
    while prev < n:
        pos = max(0, min(n, -(-(threshold - start) // step)))
        if pos > prev:
            levels.append((prev, pos))
            prev = pos
        threshold *= c
    return levels


def _key_levels(spec, plan, env, module_globals) -> Optional[List[Tuple[int, int]]]:
    depend = spec.clauses.get("depend")
    order_name = depend.value.kwargs["order"].value
    key_fn = preflight._resolve(order_name, env, module_globals)
    if key_fn is None:
        return None
    values = list(plan.base) if plan.domain == "range" else list(range(plan.n))
    keyed = sorted(range(plan.n), key=lambda p: key_fn(values[p]))
    if keyed != list(range(plan.n)):
        return None
    levels: List[Tuple[int, int]] = []
    prev_key, start_pos = None, 0
    for pos in range(plan.n):
        key = key_fn(values[pos])
        if prev_key is None:
            prev_key = key
        elif key != prev_key:
            levels.append((start_pos, pos))
            start_pos, prev_key = pos, key
    levels.append((start_pos, plan.n))
    return levels
