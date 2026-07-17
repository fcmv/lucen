from __future__ import annotations

import array
import atexit
import builtins
import os
import sys
import threading
import time
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

from lucen.analysis.scanner import ClauseValue
from lucen.analysis.selector import BlockDecision, Eligibility, _strict_requests_hard
from lucen.codegen import ChunkArtifact
from lucen.execution import nested_guard, preflight
from lucen.execution.affinity import _apply_affinity
from lucen.execution.planning import _bounds, _plan_domain, _Record
from lucen.execution.runtime import (
    SKIP,
    assign_path,
    audit_disjoint_dict_slabs,
    commit_dict_slab,
    commit_list_slab,
    fold_contributions,
    resolve_path,
)
from lucen.support import config, costmodel
from lucen.support.errors import (
    MidRunSerializationError,
    ParallelTimeoutError,
    ParallelWriteConflictError,
    PreflightCheckError,
    UnprofitableParallelismError,
    raise_or_fallback,
    report_fallback,
)

_EXTRA_PARAMS = frozenset(
    {"_plx_errors", "_plx_clock", "_plx_deadline", "_plx_timeout_error", "_plx_progress"}
)

_pool: Optional[ThreadPoolExecutor] = None
_pool_size = 0
_pool_lock = threading.Lock()
_memo_lock = threading.Lock()
_stats_lock = threading.Lock()
_memo: Dict[Tuple[str, int], Tuple[float, int, int]] = {}
_collected: Dict[Tuple[str, int], List[Tuple[int, BaseException]]] = {}
_stats: Dict[Tuple[str, int], Dict[str, Any]] = {}

_MEMO_MAX_USES = 64
_MEMO_REGIME_FACTOR = 10


def free_threaded() -> bool:
    probe = getattr(sys, "_is_gil_enabled", None)
    return probe is not None and not probe()


_MIN_RECURSION_HEADROOM = 100


def _recursion_headroom() -> int:
    depth = 2
    frame = sys._getframe(1)
    while frame is not None:
        depth += 1
        frame = frame.f_back
    return sys.getrecursionlimit() - depth


def _ensure_pool(size: int) -> ThreadPoolExecutor:
    global _pool, _pool_size
    with _pool_lock:
        if _pool is None:
            _pool_size = size
            _pool = ThreadPoolExecutor(max_workers=size, thread_name_prefix="lucen")
            atexit.register(shutdown)
        return _pool


def shutdown() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=True)
            _pool = None


def get_collected_errors(key=None):
    if key is not None:
        return list(_collected.get(key, ()))
    return {k: list(v) for k, v in _collected.items()}


def get_block_stats() -> Dict[Tuple[str, int], Dict[str, Any]]:
    return {k: dict(v) for k, v in _stats.items()}


def reset_runtime_state() -> None:
    _memo.clear()
    _collected.clear()
    _stats.clear()


@dataclass
class BlockSpec:
    filename: str
    line: int
    artifact: ChunkArtifact
    eligibility: Eligibility
    dag_divisor: Optional[int]
    reduction_ops: Dict[str, str]
    static_unprofitable: bool
    clauses: Dict[str, ClauseValue]
    skip_runtime_check: bool
    arg_names: Tuple[str, ...]
    grainsize: int
    called_paths: Tuple[str, ...] = ()
    trusted_names: FrozenSet[str] = frozenset()
    _fns: Optional[Tuple[Callable, Callable]] = field(default=None, repr=False)

    @property
    def key(self) -> Tuple[str, int]:
        return (self.filename, self.line)

    def fns(self) -> Tuple[Callable, Callable]:
        if self._fns is None:
            self._fns = self.artifact.compile_pair()
        return self._fns


def make_spec(analysis, decision: BlockDecision, artifact: ChunkArtifact) -> BlockSpec:
    clauses = analysis.block.start.clauses
    special = (
        {"_plx_indices", "_plx_seq"}
        | _EXTRA_PARAMS
        | {p.param for p in artifact.slabs}
        | {p for r in artifact.reductions for p in r.site_params}
    )
    names = {p for p in artifact.params if p not in special}
    names |= {p for p in artifact.seq_params if p not in ("_plx_iter", "_plx_skip")}
    names |= {p.container.split(".", 1)[0] for p in artifact.slabs}
    names |= {r.scalar.split(".", 1)[0] for r in artifact.reductions}
    src = clauses.get("skip_runtime_check")
    grain = clauses.get("grainsize")
    if grain is not None:
        grainsize = grain.value if grain.kind == "literal" else grain.value.base.value
    else:
        grainsize = 1024
    return BlockSpec(
        filename=analysis.filename,
        line=analysis.block.start.lineno,
        artifact=artifact,
        eligibility=decision.eligibility,
        dag_divisor=decision.dag_divisor,
        reduction_ops=dict(decision.reduction_ops),
        static_unprofitable=decision.unprofitable,
        clauses=dict(clauses),
        skip_runtime_check=bool(src is not None and src.value is True),
        arg_names=tuple(sorted(names)),
        grainsize=grainsize,
        called_paths=tuple(sorted(analysis.called_paths)),
        trusted_names=frozenset(analysis.trusted_names),
    )


def _zero_stats(runs: int) -> Dict[str, Any]:
    return {
        "runs": runs,
        "parallel_runs": 0,
        "sequential_runs": 0,
        "chunks": 0,
        "workers": 0,
        "backend": "",
        "probe_ns": None,
        "duration_ns": 0,
        "fallback_runs": 0,
    }


def _fresh_stats() -> Dict[str, Any]:
    return _zero_stats(runs=1)


_STATS_SUMS = ("runs", "parallel_runs", "sequential_runs", "chunks", "fallback_runs", "duration_ns")


# accumulate locally, merge once under the lock: concurrent same-block runs
# must not lose increments
def _merge_stats(key, local) -> None:
    with _stats_lock:
        agg = _stats.setdefault(key, _zero_stats(runs=0))
        for k in _STATS_SUMS:
            agg[k] += local[k]
        agg["workers"] = max(agg["workers"], local["workers"])
        agg["backend"] = local["backend"] or agg["backend"]
        if local["probe_ns"] is not None:
            agg["probe_ns"] = local["probe_ns"]


def execute(
    spec: BlockSpec,
    iterable,
    env: Dict[str, Any],
    module_globals: Optional[Dict[str, Any]] = None,
    force_backend: Optional[str] = None,
) -> Optional[tuple]:
    stats = _fresh_stats()
    t_start = time.perf_counter_ns()
    try:
        return _execute(spec, iterable, env, module_globals, force_backend, stats)
    finally:
        stats["duration_ns"] += time.perf_counter_ns() - t_start
        _merge_stats(spec.key, stats)


def _execute(spec, iterable, env, module_globals, force_backend, stats):
    plan = _plan_domain(spec.artifact, iterable)
    if plan.n == 0:
        return None

    if nested_guard.active():
        report_fallback(
            "nested parallel region: inner block runs SEQUENTIAL (spec 5.11)",
            file=spec.filename,
            line=spec.line,
            error="NestedParallelRegion",
        )
        return _run_twin(spec, plan, env, module_globals, stats)

    # dispatch and pickle depth must never blow a user-lowered limit the loop
    # body itself fits under; the twin adds only a few frames
    if _recursion_headroom() < _MIN_RECURSION_HEADROOM:
        report_fallback(
            f"recursion headroom below {_MIN_RECURSION_HEADROOM} frames "
            "(sys.setrecursionlimit); parallel machinery needs more, ran "
            "SEQUENTIAL",
            file=spec.filename,
            line=spec.line,
            error="RecursionHeadroom",
        )
        return _run_twin(spec, plan, env, module_globals, stats)

    gate = preflight.check(spec, env, module_globals)
    if not gate.ok:
        raise_or_fallback(gate.refusal)
        stats["fallback_runs"] += 1
        return _run_twin(spec, plan, env, module_globals, stats)

    backend = force_backend or _pick_backend(spec)
    if backend == "sequential":
        return _run_twin(spec, plan, env, module_globals, stats)

    workers, n_chunks = _sizing(spec, plan.n, backend)
    bounds = _bounds(plan.n, n_chunks)
    deadline = _deadline(spec)
    _apply_affinity(spec, workers)

    if spec.eligibility is Eligibility.EARLY_EXIT:
        from lucen.execution import early_exit

        return early_exit.execute_early_exit(
            spec, plan, env, module_globals, gate, workers, backend, stats
        )

    if spec.eligibility is Eligibility.WAVEFRONT:
        if backend == "process" and not _explicit_backend(spec):
            report_fallback(
                "recognized-DAG wavefront runs SEQUENTIAL by default (PROCESS "
                "per-level dispatch is slower; force backend=thread to run the "
                "wavefront on a free-threaded build, spec 5.6)",
                file=spec.filename,
                line=spec.line,
                error="WavefrontSequentialDefault",
            )
            return _run_twin(spec, plan, env, module_globals, stats)
        from lucen.execution import wavefront

        try:
            return wavefront.execute_wavefront(
                spec, plan, env, module_globals, gate, workers, backend, stats
            )
        except (PreflightCheckError, MidRunSerializationError) as exc:
            raise_or_fallback(exc)
            stats["fallback_runs"] += 1
            return _run_twin(spec, plan, env, module_globals, stats)

    # chunk 0 may already have run during the probe: a chunk-fn probe carries
    # its slab in probe_record, a twin probe wrote env in place
    probe_record: Optional[_Record] = None
    probed_end = 0
    mode = _calibrate_mode(spec)
    if spec.static_unprofitable and mode != "false":
        _handle_unprofitable(spec, None, gate)
        return _run_twin(spec, plan, env, module_globals, stats)
    if mode in ("auto", "always"):
        t_ns = _memo_lookup(spec, plan.n) if mode == "auto" else None
        if t_ns is None:
            if _twin_probe_ok(spec):
                t_ns = _probe_twin(spec, plan, bounds[0], env, module_globals)
            else:
                probe_record, t_ns = _probe(spec, plan, bounds[0], env, module_globals, gate)
            probed_end = bounds[0][1]
            stats["probe_ns"] = t_ns
            if mode == "auto":
                with _memo_lock:
                    _memo[spec.key] = (t_ns, plan.n, 0)
            if probe_record is not None and probe_record.error is not None:
                _commit_records(spec, plan, [probe_record], env, module_globals, gate)
                raise probe_record.error
        if (
            force_backend is None
            and backend == "process"
            and free_threaded()
            and _explicit_backend(spec) is None
            and t_ns >= costmodel.FT_THREAD_MIN_NS
        ):
            backend = "thread"
        remaining = plan.n - probed_end
        if not _profitable(spec, t_ns, remaining, workers, len(bounds), backend):
            _handle_unprofitable(spec, t_ns, gate)
            if probe_record is not None:
                _commit_records(spec, plan, [probe_record], env, module_globals, gate)
            return _run_twin(spec, plan, env, module_globals, stats, start=probed_end)

    if (
        spec.artifact.buffer_fast_path
        and backend == "thread"
        and _direct_write_ready(spec, env, module_globals)
    ):
        return _run_buffer_direct(
            spec,
            plan,
            env,
            module_globals,
            gate,
            workers,
            deadline,
            stats,
            probe_record,
            bounds,
            probed_end,
        )

    rest = bounds[1:] if probed_end else bounds
    try:
        records, first_error = _run_chunk_set(
            spec, plan, rest, env, module_globals, gate, workers, backend, deadline, stats
        )
    except (PreflightCheckError, MidRunSerializationError) as exc:
        raise_or_fallback(exc)
        stats["fallback_runs"] += 1
        if probe_record is not None:
            _commit_records(spec, plan, [probe_record], env, module_globals, gate)
        return _run_twin(spec, plan, env, module_globals, stats, start=probed_end)
    if probe_record is not None:
        records = [probe_record] + records
    return _join(spec, plan, records, first_error, env, module_globals, gate, stats)


def _join(spec, plan, records, first_error, env, module_globals, gate, stats):
    records.sort(key=lambda r: r.idx)
    if first_error is not None:
        prefix = [r for r in records if r.idx <= first_error[0]]
        _commit_records(spec, plan, prefix, env, module_globals, gate)
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
                conflict = ParallelWriteConflictError(
                    f"chunks wrote '{slab_plan.container}[{overlap!r}]' more "
                    "than once; discarding the parallel attempt and re-running "
                    "sequentially",
                    file=spec.filename,
                    line=spec.line,
                )
                if _fallback_override(spec, "conflict") == "hard":
                    raise conflict
                record = raise_or_fallback(conflict)
                if gate.on_fallback_handler is not None:
                    gate.on_fallback_handler(record)
                stats["fallback_runs"] += 1
                return _run_twin(spec, plan, env, module_globals, stats)

    _commit_records(spec, plan, records, env, module_globals, gate)
    _flush_collected(spec, records, gate)
    _emit_progress(spec, gate, records, plan.n, base_completed=0)
    stats["parallel_runs"] += 1
    stats["chunks"] += len(records)
    return _rebind(spec, plan, env, module_globals)


class _ChunkFailure(Exception):
    def __init__(self, record: "_Record"):
        super().__init__()
        self.record = record


def _run_chunk_set(
    spec, plan, bounds, env, module_globals, gate, workers, backend, deadline, stats
):
    if backend == "process":
        from lucen.execution import process_backend

        return process_backend.run_chunks(
            spec, plan, bounds, env, module_globals, gate, workers, deadline, stats
        )
    pool = _ensure_pool(max(workers, os.cpu_count() or 4))
    stats["workers"] = max(stats["workers"], min(workers, len(bounds)))
    stats["backend"] = "thread"
    chunk_fn, _ = spec.fns()
    fail_fast = (
        gate.on_error_handler is None
        and gate.max_errors is None
        and not spec.artifact.collect_errors
    )
    budget = threading.Semaphore(workers)

    def job(idx: int, a: int, b: int) -> _Record:
        record = _new_record(spec, plan, idx, a, b)
        args = _chunk_args(spec, plan, record, env, module_globals, gate, deadline)
        with budget, nested_guard.dispatch_scope():
            try:
                chunk_fn(*args)
            except BaseException as exc:  # noqa: BLE001 - rethrown at join
                record.error = exc
        if record.error is not None and fail_fast:
            raise _ChunkFailure(record)
        return record

    futures = [pool.submit(job, idx, a, b) for idx, (a, b) in enumerate(bounds, start=1)]
    timeout_s = max(0.0, deadline - time.monotonic()) if deadline is not None else None
    done, not_done = wait(futures, timeout=timeout_s, return_when=FIRST_EXCEPTION)

    # return_when=FIRST_EXCEPTION returns early only on a raise or all-complete;
    # a non-empty not_done with nothing raised therefore means the wait elapsed,
    # which is the timeout. Deciding on that (rather than re-reading the clock
    # against the deadline the wait just reached) removes a scheduler-jitter race.
    raised = any(f.exception() is not None for f in done)
    if not_done and deadline is not None and not raised:
        for fut in not_done:
            fut.cancel()
        finished, _ = wait([f for f in not_done if not f.cancelled()])
        records = sorted(
            (_record_of(f) for f in done | finished if not f.cancelled()), key=lambda r: r.idx
        )
        _commit_records(spec, plan, _contiguous_prefix(records), env, module_globals, gate)
        exc = ParallelTimeoutError(
            "block exceeded its timeout= bound (cooperative on THREAD: "
            "running chunks finished first)",
            file=spec.filename,
            line=spec.line,
        )
        if gate.on_timeout_handler is not None:
            gate.on_timeout_handler(exc)
        raise exc

    if not_done:
        for fut in not_done:
            fut.cancel()
        finished, _ = wait([f for f in not_done if not f.cancelled()])
        done |= finished

    records = [_record_of(f) for f in done if not f.cancelled()]
    first_error = None
    errored = sorted((r for r in records if r.error is not None), key=lambda r: r.idx)
    if errored:
        first_error = (errored[0].idx, errored[0].error)

    if gate.max_errors is not None and first_error is None:
        total = sum(len(r.errors) for r in records)
        if total > gate.max_errors:
            report_fallback(
                f"on_error collect exceeded max_errors={gate.max_errors}",
                file=spec.filename,
                line=spec.line,
                error="MaxErrorsExceeded",
            )
    return records, first_error


def _record_of(future) -> "_Record":
    try:
        return future.result()
    except _ChunkFailure as failure:
        return failure.record


def _new_record(spec, plan, idx, a, b) -> _Record:
    slabs = {}
    for slab_plan in spec.artifact.slabs:
        slabs[slab_plan.param] = [SKIP] * (b - a) if slab_plan.kind == "list" else {}
    sites = {p: [SKIP] * (b - a) for r in spec.artifact.reductions for p in r.site_params}
    return _Record(idx, a, b, slabs, sites, errors=[])


def _chunk_args(spec, plan, record, env, module_globals, gate, deadline):
    return [
        _one_arg(p, spec, plan, record, env, module_globals, gate, deadline)
        for p in spec.artifact.params
    ]


def _one_arg(p, spec, plan, record, env, module_globals, gate, deadline):
    if p == "_plx_indices":
        return plan.indices(record.a, record.b)
    if p == "_plx_seq":
        return plan.seq
    if p in record.slabs:
        return record.slabs[p]
    if p in record.sites:
        return record.sites[p]
    if p == "_plx_errors":
        return record.errors
    if p == "_plx_clock":
        return time.monotonic
    if p == "_plx_deadline":
        return deadline if deadline is not None else float("inf")
    if p == "_plx_timeout_error":
        return ParallelTimeoutError(
            "per-iteration timeout= deadline exceeded", file=spec.filename, line=spec.line
        )
    if p == "_plx_progress":
        return gate.progress_cb or (lambda _i: None)
    return _value_of(p, env, module_globals)


def _commit_records(spec, plan, records, env, module_globals, gate) -> None:
    for record in sorted(records, key=lambda r: r.idx):
        for slab_plan in spec.artifact.slabs:
            container = resolve_path(env, slab_plan.container)
            slab = record.slabs[slab_plan.param]
            if slab_plan.kind == "list":
                commit_list_slab(container, plan.indices(record.a, record.b), slab)
            else:
                commit_dict_slab(container, slab)
        for red in spec.artifact.reductions:
            current = resolve_path(env, red.scalar)
            op = gate.reduce_fn if red.op == "custom" and gate.reduce_fn else red.op
            current = fold_contributions(current, [record.sites[p] for p in red.site_params], op)
            assign_path(env, red.scalar, current)


def _flush_collected(spec, records, gate) -> None:
    if not spec.artifact.collect_errors:
        return
    merged: List[Tuple[int, BaseException]] = []
    for record in sorted(records, key=lambda r: r.idx):
        merged.extend(record.errors)
    _collected[spec.key] = merged
    if gate.on_error_handler is not None:
        for index, exc in merged:
            gate.on_error_handler(index, exc)


def _rebind(spec, plan, env, module_globals) -> tuple:
    artifact = spec.artifact
    ns: Dict[str, Any] = {"_v": plan.last_element()}
    exec(f"{artifact.target_source} = _v", {}, ns)
    values = [ns[t] for t in artifact.loop_targets]
    for red in artifact.reductions:
        if "." not in red.scalar:
            values.append(env[red.scalar])
    return tuple(values)


_BUFFER_TYPES: Tuple[type, ...] = (array.array, bytearray, memoryview)


def _direct_write_ready(spec, env, module_globals) -> bool:
    for slab_plan in spec.artifact.slabs:
        try:
            container = resolve_path(env, slab_plan.container)
        except (KeyError, AttributeError):
            return False
        if not (type(container) is list or isinstance(container, _BUFFER_TYPES)):
            return False
    return True


def _run_buffer_direct(
    spec,
    plan,
    env,
    module_globals,
    gate,
    workers,
    deadline,
    stats,
    probe_record,
    bounds,
    probed_end,
) -> Optional[tuple]:
    _, seq_fn = spec.fns()
    pool = _ensure_pool(max(workers, os.cpu_count() or 4))
    stats["backend"] = "thread"
    stats["workers"] = max(stats["workers"], min(workers, len(bounds)))
    budget = threading.Semaphore(workers)
    start = probed_end

    if probe_record is not None:
        _commit_records(spec, plan, [probe_record], env, module_globals, gate)

    def job(a: int, b: int) -> None:
        args = []
        for p in spec.artifact.seq_params:
            if p == "_plx_iter":
                args.append(plan.sub_iter(a, b))
            elif p == "_plx_skip":
                args.append(SKIP)
            else:
                args.append(_value_of(p, env, module_globals))
        with budget, nested_guard.dispatch_scope():
            seq_fn(*args)

    work = [(a, b) for a, b in bounds if b > start]
    if work and work[0][0] < start:
        work[0] = (start, work[0][1])
    futures = [pool.submit(job, a, b) for a, b in work]
    for fut in futures:
        exc = fut.exception()
        if exc is not None:
            raise exc
    stats["parallel_runs"] += 1
    stats["chunks"] += len(work)
    return _rebind(spec, plan, env, module_globals)


def _run_twin(spec, plan, env, module_globals, stats, start=0) -> Optional[tuple]:
    stats["sequential_runs"] += 1
    stats["backend"] = stats["backend"] or "sequential"
    _, seq_fn = spec.fns()
    args = []
    for p in spec.artifact.seq_params:
        if p == "_plx_iter":
            args.append(plan.remaining_iter(start))
        elif p == "_plx_skip":
            args.append(SKIP)
        else:
            args.append(_value_of(p, env, module_globals))
    result = seq_fn(*args)
    if result is None:
        return None
    values = list(result)
    n_targets = len(spec.artifact.loop_targets)
    if start > 0 and values and values[0] is SKIP:
        ns: Dict[str, Any] = {"_v": plan.last_element()}
        exec(f"{spec.artifact.target_source} = _v", {}, ns)
        values[:n_targets] = [ns[t] for t in spec.artifact.loop_targets]
    for offset, red in enumerate(r for r in spec.artifact.reductions if "." not in r.scalar):
        env[red.scalar] = values[n_targets + offset]
    return tuple(values)


def _probe(spec, plan, first_bounds, env, module_globals, gate):
    a, b = first_bounds
    record = _new_record(spec, plan, 0, a, b)
    args = _chunk_args(spec, plan, record, env, module_globals, gate, None)
    chunk_fn, _ = spec.fns()
    t0 = time.perf_counter_ns()
    try:
        chunk_fn(*args)
    except BaseException as exc:  # noqa: BLE001 - sequential-prefix semantics
        record.error = exc
    elapsed = time.perf_counter_ns() - t0
    return record, elapsed / max(b - a, 1)


# reductions are excluded: their twin returns the accumulator instead of
# writing output in place
def _twin_probe_ok(spec) -> bool:
    artifact = spec.artifact
    return not artifact.reductions and all(p.kind != "dict" for p in artifact.slabs)


def _probe_twin(spec, plan, first_bounds, env, module_globals):
    a, b = first_bounds
    _, seq_fn = spec.fns()
    args = []
    for p in spec.artifact.seq_params:
        if p == "_plx_iter":
            args.append(plan.sub_iter(a, b))
        elif p == "_plx_skip":
            args.append(SKIP)
        else:
            args.append(_value_of(p, env, module_globals))
    t0 = time.perf_counter_ns()
    seq_fn(*args)
    elapsed = time.perf_counter_ns() - t0
    return elapsed / max(b - a, 1)


def _memo_lookup(spec, n: int) -> Optional[float]:
    with _memo_lock:
        entry = _memo.get(spec.key)
        if entry is None:
            return None
        t_ns, seen_n, uses = entry
        if uses >= _MEMO_MAX_USES:
            return None
        if seen_n and (n > seen_n * _MEMO_REGIME_FACTOR or seen_n > n * _MEMO_REGIME_FACTOR):
            return None
        _memo[spec.key] = (t_ns, seen_n, uses + 1)
        return t_ns


def _profitable(
    spec, t_ns: float, remaining: int, workers: int, n_chunks: int, backend: str
) -> bool:
    if remaining <= 0:
        return False
    min_gain = 1.0
    calibrate = spec.clauses.get("calibrate")
    if calibrate is not None and calibrate.kind == "call":
        min_gain = float(calibrate.value.kwargs["min_gain"].value)
    gain = t_ns * remaining * (1.0 - 1.0 / max(workers, 2))
    overhead = costmodel.overhead_ns(n_chunks, remaining, thread=(backend == "thread"))
    return gain > overhead * min_gain


def _handle_unprofitable(spec, t_ns: Optional[float], gate=None) -> None:
    if t_ns is None:
        message = (
            "statically predicted to lose to dispatch overhead; ran "
            "SEQUENTIAL (calibrate=false overrides, spec 5.17)"
        )
    else:
        message = (
            f"measured ~{t_ns:.0f} ns/iteration loses to dispatch "
            "overhead; ran SEQUENTIAL (calibrate=false overrides, spec 5.17)"
        )
    if (
        _strict_requests_hard(spec.clauses.get("strict"), "unprofitable")
        or _fallback_override(spec, "unprofitable") == "hard"
    ):
        raise UnprofitableParallelismError(message, file=spec.filename, line=spec.line)
    record = report_fallback(
        message, file=spec.filename, line=spec.line, error="PARALLEL_UNPROFITABLE"
    )
    if gate is not None and gate.on_fallback_handler is not None:
        gate.on_fallback_handler(record)


def _fallback_override(spec, reason_key: str) -> Optional[str]:
    cv = spec.clauses.get("on_fallback")
    if cv is None:
        return None
    if cv.kind == "name":
        return cv.value
    base = cv.value.base.value
    if base == "custom":
        return None
    allow = cv.value.kwargs.get("allow")
    if allow is not None and reason_key in {item.value for item in allow.value}:
        return "report"
    return base


def _emit_progress(spec, gate, records, total: int, base_completed: int) -> None:
    cv = spec.clauses.get("progress")
    if cv is None or spec.artifact.progress_per_task:
        return
    completed = base_completed
    for record in sorted(records, key=lambda r: r.idx):
        completed += record.b - record.a
        if gate is not None and gate.progress_cb is not None:
            gate.progress_cb(completed, total)
        elif cv.kind == "literal" and cv.value is True:
            print(
                f"lucen: {spec.filename}:{spec.line}: {completed}/{total} iterations",
                file=sys.stderr,
            )


def _explicit_backend(spec) -> Optional[str]:
    backend = spec.clauses.get("backend")
    if backend is None:
        return None
    base = backend.value if backend.kind == "name" else backend.value.base.value
    return base if base in ("thread", "process", "sequential") else None


def _pick_backend(spec) -> str:
    backend = spec.clauses.get("backend")
    if backend is not None:
        base = backend.value if backend.kind == "name" else backend.value.base.value
        if base in ("thread", "process", "sequential"):
            return base
    if spec.artifact.inplace_mutation:
        return "thread"
    # nothing to ship back: effects are by reference, a process copy loses them
    if not spec.artifact.slabs and not spec.artifact.reductions:
        return "thread"
    # unsliceable structured reads would ship the whole container per chunk
    if spec.artifact.structured_payload and not spec.artifact.sliceable:
        return "thread"
    return "process"


def _sizing(spec, n: int, backend: str) -> Tuple[int, int]:
    cfg = config.active()
    workers = None
    chunks = None
    backend_cv = spec.clauses.get("backend")
    if backend_cv is not None and backend_cv.kind == "call":
        kwargs = backend_cv.value.kwargs
        if "pool_size" in kwargs:
            workers = kwargs["pool_size"].value
        if "chunks" in kwargs:
            chunks = kwargs["chunks"].value or None
    if workers is None:
        workers = cfg.default_for("pool_size") or (os.cpu_count() or 4)
    ceiling = cfg.max_processes_per_block if backend == "process" else cfg.max_threads_per_block
    workers = config.clamp(workers, ceiling, "pool_size", spec.filename, spec.line)
    if chunks is None:
        per_worker = (
            costmodel.PROCESS_CHUNKS_PER_WORKER
            if backend == "process"
            else costmodel.CHUNKS_PER_WORKER
        )
        chunks = cfg.default_for("chunks") or per_worker * workers
    return workers, min(chunks, n)


def _deadline(spec) -> Optional[float]:
    timeout = spec.clauses.get("timeout")
    if timeout is None:
        return None
    seconds = timeout.value if timeout.kind == "literal" else timeout.value.base.value
    cfg = config.active()
    if cfg.max_timeout_seconds is not None and seconds > cfg.max_timeout_seconds:
        seconds = config.clamp(
            seconds, cfg.max_timeout_seconds, "timeout", spec.filename, spec.line
        )
    return time.monotonic() + float(seconds)


def _calibrate_mode(spec) -> str:
    cv = spec.clauses.get("calibrate")
    if cv is None:
        mode = config.active().default_for("calibrate") or "auto"
        return str(mode)
    if cv.kind == "literal":
        return "false" if cv.value is False else "auto"
    if cv.kind == "name":
        return cv.value
    return "auto"


def _contiguous_prefix(records: List[_Record]) -> List[_Record]:
    prefix: List[_Record] = []
    expected = records[0].idx if records else 0
    for record in records:
        if record.idx != expected or record.error is not None:
            break
        prefix.append(record)
        expected += 1
    return prefix


def _value_of(name: str, env: Dict[str, Any], module_globals: Optional[Dict[str, Any]]) -> Any:
    if name in env:
        return env[name]
    if module_globals and name in module_globals:
        return module_globals[name]
    return getattr(builtins, name)
