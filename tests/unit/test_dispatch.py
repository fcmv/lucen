from __future__ import annotations

import ast
import copy
import sys
import time

import pytest

from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import select
from lucen.codegen import generate
from lucen.execution import dispatch, nested_guard, preflight
from lucen.execution.dispatch import execute, make_spec
from lucen.execution.planning import _plan_domain, _Record
from lucen.support import config
from lucen.support.errors import (
    ErrorsMode,
    ParallelTimeoutError,
    clear_fallback_report,
    get_fallback_report,
    set_errors_mode,
)


@pytest.fixture(autouse=True)
def _clean_state():
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config())
    yield
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config())


def build(src: str):
    scan = scan_source(src, "t.py")
    analyses = analyze_source(src, scan, "t.py")
    assert len(analyses) == 1
    analysis = analyses[0]
    decision = select(analysis, workers=8)
    artifact = generate(analysis, decision, "t.py")
    assert artifact is not None
    return analysis, make_spec(analysis, decision, artifact)


def run(src: str, env: dict, backend: str = "thread"):
    analysis, spec = build(src)
    env = copy.deepcopy(env)
    if spec.artifact.domain == "range":
        iterable = eval(ast.unparse(analysis.for_node.iter), dict(env))
    elif spec.artifact.domain == "enumerate":
        iterable = eval(ast.unparse(analysis.for_node.iter.args[0]), dict(env))
    else:
        iterable = eval(ast.unparse(analysis.for_node.iter), dict(env))
    result = execute(spec, iterable, env, force_backend=backend)
    return env, result, spec


def golden(src: str, env: dict) -> dict:
    g = copy.deepcopy(env)
    exec(src, g)
    return g


def block(body_lines, header="for i in range(1, n):", clauses=""):
    suffix = f" {clauses}" if clauses else ""
    body = "\n".join("    " + line for line in body_lines)
    return f"# LUCEN START{suffix}\n{header}\n{body}\n# LUCEN END\n"


def test_worker_exhaustion_falls_back_instead_of_crashing():
    # A pool hands out workers lazily, so a thread ceiling surfaces as a failed
    # submit partway through dispatch. That must degrade to a reported
    # sequential run, not surface as a crash in the middle of the loop.
    src = block(
        ["ys[i] = xs[i] * 2"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, backend=thread(chunks=4)",
    )
    env = {"xs": list(range(40)), "ys": [0] * 40}
    pool = dispatch._ensure_pool(4)
    original, calls = pool.submit, []

    def exhausted(*args, **kwargs):
        calls.append(1)
        if len(calls) > 2:
            raise RuntimeError("can't start new thread")
        return original(*args, **kwargs)

    pool.submit = exhausted
    try:
        got, _, spec = run(src, env)
    finally:
        pool.submit = original
    assert got["ys"] == [v * 2 for v in range(40)]
    assert any(r.error == "PreflightCheckError" for r in get_fallback_report())
    assert dispatch.get_block_stats()[spec.key]["sequential_runs"] == 1


def test_interrupt_drains_workers_before_unwinding():
    # An interrupt in the collecting thread must not leave chunks running: they
    # would keep writing into the caller's containers after the exception has
    # been handled. Every future is settled by the time it propagates.
    import concurrent.futures as cf

    started, finished = [], []

    def slow(v):
        started.append(v)
        time.sleep(0.01)
        finished.append(v)
        return v * 2

    src = block(
        ["ys[i] = slow(xs[i])"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, backend=thread(chunks=4), trust=callables",
    )
    env = {"xs": list(range(40)), "ys": [0] * 40, "slow": slow}
    original, calls = cf.Future.exception, []

    def interrupting(self, timeout=None):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return original(self, timeout=timeout)

    cf.Future.exception = interrupting
    try:
        with pytest.raises(KeyboardInterrupt):
            run(src, env)
    finally:
        cf.Future.exception = original
    assert len(started) == len(finished), "a chunk was still running after the interrupt"
    stats = list(dispatch.get_block_stats().values())[-1]
    assert stats["interrupted_runs"] == 1


def test_thread_map_equivalence():
    src = block(
        ["ys[i] = xs[i] * 3 + 1"], header="for i in range(len(xs)):", clauses="calibrate=false"
    )
    env = {"xs": list(range(5000)), "ys": [0] * 5000}
    p, result, _ = run(src, env)
    g = golden(src, env)
    assert p["ys"] == g["ys"]
    assert result == (4999,)


def test_reduction_bit_identity_and_rebind():
    src = block(
        ["total += vals[i] * 1.0001"],
        header="for i in range(len(vals)):",
        clauses="calibrate=false",
    )
    env = {"vals": [0.1 * k + 0.007 for k in range(3000)], "total": 1.5}
    p, result, _ = run(src, env)
    g = golden(src, env)
    assert p["total"] == g["total"]
    assert result == (2999, g["total"])


def _fixed_probe(ns: float, seen: list):
    """Report a fixed cost per iteration, but still run chunk 0.

    The twin probe writes the first chunk into env and dispatch skips it
    afterwards, so a stub that only returns a number would leave that chunk
    unwritten and the block would commit a hole.
    """
    real = dispatch._probe_twin

    def probe(spec, plan, first_bounds, env, module_globals):
        real(spec, plan, first_bounds, env, module_globals)
        seen.append(spec.key)
        return ns

    return probe


def test_probe_gates_tiny_block_and_stays_correct(monkeypatch):
    # What the gate decides is under test, not what the clock says. A real probe
    # times one chunk once, a few dozen of these 300 iterations and so a few
    # microseconds; one preemption on a shared runner inflates that past the
    # threshold and the block routes parallel. Feeding the measurement keeps the
    # routing, the report, and the commit real while the decision is fixed.
    src = block(["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):")
    env = {"xs": list(range(300)), "ys": [0] * 300}
    probed: list = []
    monkeypatch.setattr(dispatch, "_probe_twin", _fixed_probe(50.0, probed))
    p, _, spec = run(src, env)
    assert probed, "twin probe was not the path taken; this test no longer covers the gate"
    assert p["ys"] == golden(src, env)["ys"]
    stats = dispatch.get_block_stats()[spec.key]
    assert stats["parallel_runs"] == 0
    assert stats["sequential_runs"] == 1
    assert any(r.error == "PARALLEL_UNPROFITABLE" for r in get_fallback_report())


def test_probe_lets_an_expensive_block_through(monkeypatch):
    # The profitable half of the same gate. Every other test that asserts a
    # parallel run forces the backend with calibrate=false, which skips the
    # gate, so nothing else covers the probe deciding to parallelize.
    src = block(["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):")
    env = {"xs": list(range(300)), "ys": [0] * 300}
    probed: list = []
    monkeypatch.setattr(dispatch, "_probe_twin", _fixed_probe(50_000.0, probed))
    p, _, spec = run(src, env)
    assert probed
    assert p["ys"] == golden(src, env)["ys"]
    stats = dispatch.get_block_stats()[spec.key]
    assert stats["parallel_runs"] == 1
    assert not any(r.error == "PARALLEL_UNPROFITABLE" for r in get_fallback_report())


def test_calibration_memo_reused():
    src = block(["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):")
    env = {"xs": list(range(300)), "ys": [0] * 300}
    analysis, spec = build(src)
    for _ in range(3):
        execute(spec, range(300), copy.deepcopy(env), force_backend="thread")
    assert dispatch._memo[spec.key][2] == 2


def test_fail_fast_prefix_post_state():
    src = block(
        ["ys[i] = 100 // xs[i]"], header="for i in range(len(xs)):", clauses="calibrate=false"
    )
    xs = [1] * 500
    xs[137] = 0
    env = {"xs": xs, "ys": [-1] * 500}
    with pytest.raises(ZeroDivisionError):
        run(src, env)
    env2 = copy.deepcopy(env)
    analysis, spec = build(src)
    with pytest.raises(ZeroDivisionError):
        execute(spec, range(500), env2, force_backend="thread")
    g = copy.deepcopy(env)
    try:
        exec(src, g)
    except ZeroDivisionError:
        pass
    committed = env2["ys"][:137]
    assert committed == g["ys"][:137]
    assert env2["ys"][137] == -1


def test_write_conflict_transparent_rerun():
    src = block(["seen[key] = key * 2"], header="for key in keys:", clauses="calibrate=false")
    env = {"keys": ["a", "b", "c", "d", "a", "e", "f", "g"], "seen": {}}
    p, _, spec = run(src, env)
    assert p["seen"] == golden(src, env)["seen"]
    assert any(r.error == "ParallelWriteConflictError" for r in get_fallback_report())
    assert dispatch.get_block_stats()[spec.key]["fallback_runs"] == 1


def test_skip_runtime_check_disables_audit():
    src = block(
        ["seen[key] = key * 2"],
        header="for key in keys:",
        clauses="calibrate=false, skip_runtime_check=true",
    )
    env = {"keys": ["a", "b", "c", "d", "a", "e", "f", "g"], "seen": {}}
    p, _, _ = run(src, env)
    assert p["seen"] == golden(src, env)["seen"]
    assert not any(r.error == "ParallelWriteConflictError" for r in get_fallback_report())


def test_on_error_collect_gathers_and_continues():
    src = block(
        ["ys[i] = 100 // xs[i]"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, on_error=collect",
    )
    xs = [1] * 200
    xs[13] = 0
    xs[150] = 0
    env = {"xs": xs, "ys": [-1] * 200}
    p, _, spec = run(src, env)
    errors = dispatch.get_collected_errors(spec.key)
    assert [idx for idx, _ in errors] == [13, 150]
    assert all(isinstance(e, ZeroDivisionError) for _, e in errors)
    assert p["ys"][14] == 100
    assert p["ys"][13] == -1


def test_on_error_custom_handler_called():
    calls = []
    src = block(
        ["ys[i] = 100 // xs[i]"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, on_error=custom(handler=record_it)",
    )
    xs = [1] * 100
    xs[42] = 0
    env = {"xs": xs, "ys": [0] * 100, "record_it": lambda idx, exc: calls.append(idx)}
    run(src, env)
    assert calls == [42]


def test_timeout_whole_block_raises():
    # Total work (16 * 0.2s = 3.2s over two workers) far exceeds the 0.3s
    # deadline, so chunks are still running when the wait elapses regardless of
    # machine speed or load, and the timeout fires deterministically.
    src = block(
        ["ys[i] = crawl(xs[i])"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, backend=thread(pool_size=2), timeout=0.3",
    )
    env = {"xs": list(range(16)), "ys": [0] * 16, "crawl": lambda v: (time.sleep(0.2), v)[1]}
    with pytest.raises(ParallelTimeoutError):
        run(src, env)


def test_nested_guard_forces_sequential():
    src = block(["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):", clauses="calibrate=false")
    analysis, spec = build(src)
    env = {"xs": list(range(2000)), "ys": [0] * 2000}
    with nested_guard.dispatch_scope():
        execute(spec, range(2000), env, force_backend="thread")
    assert env["ys"] == golden(src, {"xs": env["xs"], "ys": [0] * 2000})["ys"]
    stats = dispatch.get_block_stats()[spec.key]
    assert stats["sequential_runs"] == 1
    assert any(r.error == "NestedParallelRegion" for r in get_fallback_report())


def test_wavefront_dag_equivalence():
    src = block(["out[i] = out[i // 2] + w[i]"], clauses="calibrate=false, grainsize=8")
    env = {"n": 4096, "out": [1] + [0] * 4095, "w": list(range(4096))}
    p, result, spec = run(src, env)
    g = golden(src, env)
    assert p["out"] == g["out"]
    assert result == (4095,)
    assert dispatch.get_block_stats()[spec.key]["parallel_runs"] == 1


def test_dag_on_gil_default_runs_sequential(monkeypatch):
    monkeypatch.setattr(dispatch, "free_threaded", lambda: False)
    src = block(["out[i] = out[i // 2] + w[i]"], clauses="calibrate=false")
    analysis, spec = build(src)
    env = {"n": 2048, "out": [1] + [0] * 2047, "w": list(range(2048))}
    run_env = copy.deepcopy(env)
    execute(spec, range(1, 2048), run_env)
    g = copy.deepcopy(env)
    exec(src, g)
    assert run_env["out"] == g["out"]
    st = dispatch.get_block_stats()[spec.key]
    assert st["parallel_runs"] == 0 and st["sequential_runs"] == 1


def test_dag_on_gil_explicit_process_still_runs_wavefront(monkeypatch):
    monkeypatch.setattr(dispatch, "free_threaded", lambda: False)
    src = block(
        ["out[i] = out[i // 2] + w[i]"], clauses="calibrate=false, backend=process, grainsize=8"
    )
    analysis, spec = build(src)
    env = {"n": 512, "out": [1.0] + [0.0] * 511, "w": [float(k) for k in range(512)]}
    run_env = copy.deepcopy(env)
    execute(spec, range(1, 512), run_env)
    g = copy.deepcopy(env)
    exec(src, g)
    assert run_env["out"] == g["out"]


def test_wavefront_asserted_acyclic_by_key():
    n = 90
    src_idx = [i if i < 10 else (i // 10 - 1) * 10 for i in range(n)]
    src = block(
        ["out[i] = out[srcs[i]] + 1"],
        header="for i in range(len(srcs)):",
        clauses="calibrate=false, depend=acyclic(order=bucket_of), grainsize=4",
    )
    env = {"srcs": src_idx, "out": [0] * n, "bucket_of": lambda v: v // 10}
    p, _, _ = run(src, env)
    assert p["out"] == golden(src, env)["out"]


def test_zero_length_iterable_returns_none():
    src = block(["ys[i] = xs[i]"], header="for i in range(len(xs)):", clauses="calibrate=false")
    analysis, spec = build(src)
    assert execute(spec, range(0), {"xs": [], "ys": []}, force_backend="thread") is None


def test_limits_clamp_pool_size_and_report():
    config.set_active(config.Config(max_threads_per_block=2))
    src = block(
        ["ys[i] = xs[i] + 1"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, backend=thread(pool_size=64)",
    )
    env = {"xs": list(range(1000)), "ys": [0] * 1000}
    p, _, _ = run(src, env)
    assert p["ys"] == golden(src, env)["ys"]
    assert any(r.error == "LimitClamp" for r in get_fallback_report())


def test_ft_heavy_block_promoted_to_thread(monkeypatch):
    a, spec = build(block(["ys[i] = big(xs[i])"], header="for i in range(len(xs)):"))
    n = 2000
    monkeypatch.setattr(dispatch, "free_threaded", lambda: True)
    dispatch._memo[spec.key] = (20_000.0, n, 0)
    env = {"xs": list(range(n)), "ys": [0] * n, "big": lambda v: v * 2 + 1}
    execute(spec, range(n), env)
    assert env["ys"] == [v * 2 + 1 for v in range(n)]
    assert dispatch.get_block_stats()[spec.key]["backend"] == "thread"


def test_ft_light_block_not_promoted(monkeypatch):
    a, spec = build(block(["ys[i] = xs[i] * 2"], header="for i in range(len(xs)):"))
    n = 2000
    monkeypatch.setattr(dispatch, "free_threaded", lambda: True)
    dispatch._memo[spec.key] = (100.0, n, 0)
    env = {"xs": list(range(n)), "ys": [0] * n}
    execute(spec, range(n), env)
    assert env["ys"] == [v * 2 for v in range(n)]
    assert dispatch.get_block_stats()[spec.key]["backend"] != "thread"


def test_ft_promotion_skipped_on_gil(monkeypatch):
    a, spec = build(block(["ys[i] = xs[i] * 2"], header="for i in range(len(xs)):"))
    n = 2000
    monkeypatch.setattr(dispatch, "free_threaded", lambda: False)
    dispatch._memo[spec.key] = (20_000.0, n, 0)
    env = {"xs": list(range(n)), "ys": [0] * n}
    execute(spec, range(n), env)
    assert dispatch.get_block_stats()[spec.key]["backend"] == "process"


def test_ft_explicit_process_not_promoted(monkeypatch):
    a, spec = build(
        block(["ys[i] = xs[i] * 2"], header="for i in range(len(xs)):", clauses="backend=process")
    )
    n = 2000
    monkeypatch.setattr(dispatch, "free_threaded", lambda: True)
    dispatch._memo[spec.key] = (20_000.0, n, 0)
    env = {"xs": list(range(n)), "ys": [0] * n}
    execute(spec, range(n), env)
    assert dispatch.get_block_stats()[spec.key]["backend"] == "process"


def test_ft_forced_process_not_promoted(monkeypatch):
    a, spec = build(block(["ys[i] = xs[i] * 2"], header="for i in range(len(xs)):"))
    n = 2000
    monkeypatch.setattr(dispatch, "free_threaded", lambda: True)
    dispatch._memo[spec.key] = (20_000.0, n, 0)
    env = {"xs": list(range(n)), "ys": [0] * n}
    execute(spec, range(n), env, force_backend="process")
    assert dispatch.get_block_stats()[spec.key]["backend"] == "process"


def test_low_recursion_limit_falls_back_never_crashes():
    import sys

    src = block(
        ["ys[i] = xs[i] * 2 + 1"], header="for i in range(len(xs)):", clauses="calibrate=false"
    )
    analysis, spec = build(src)
    env = {"xs": list(range(2000)), "ys": [0] * 2000}
    limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(60)
        execute(spec, range(2000), env, force_backend="process")
    finally:
        sys.setrecursionlimit(limit)
    assert env["ys"] == [v * 2 + 1 for v in range(2000)]
    assert any(r.error == "RecursionHeadroom" for r in get_fallback_report())
    assert dispatch.get_block_stats()[spec.key]["sequential_runs"] == 1


def test_twin_probe_used_for_pure_map_not_for_reduction():
    _, map_spec = build(block(["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):"))
    _, red_spec = build(block(["total += xs[i]"], header="for i in range(len(xs)):"))
    assert dispatch._twin_probe_ok(map_spec) is True
    assert dispatch._twin_probe_ok(red_spec) is False


def test_twin_probe_then_parallel_keeps_chunk0(monkeypatch):
    src = block(["ys[i] = xs[i] * 3 + 1"], header="for i in range(len(xs)):")
    analysis, spec = build(src)
    assert dispatch._twin_probe_ok(spec)
    monkeypatch.setattr(dispatch, "_profitable", lambda *a, **k: True)
    n = 6000
    env = {"xs": list(range(n)), "ys": [0] * n}
    execute(spec, range(n), env, force_backend="thread")
    assert env["ys"] == [v * 3 + 1 for v in range(n)]
    assert dispatch.get_block_stats()[spec.key]["parallel_runs"] == 1


def test_twin_probe_error_propagates_with_prefix():
    src = block(["ys[i] = 100 // xs[i]"], header="for i in range(len(xs)):")
    analysis, spec = build(src)
    n = 6000
    xs = [1] * n
    xs[40] = 0
    env = {"xs": xs, "ys": [-1] * n}
    with pytest.raises(ZeroDivisionError):
        execute(spec, range(n), env, force_backend="thread")
    assert env["ys"][:40] == [100] * 40
    assert env["ys"][40] == -1


def test_preflight_refusal_falls_back_sequentially():
    src = block(
        ["ys[i] = xs[i] + 1"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, reduce=custom(fn=nope.missing, identity=0)",
    )
    env = {"xs": list(range(50)), "ys": [0] * 50}
    p, _, spec = run(src, env)
    assert p["ys"] == golden(src, env)["ys"]
    assert any(r.error == "PreflightCheckError" for r in get_fallback_report())
    assert dispatch.get_block_stats()[spec.key]["sequential_runs"] == 1


def _record(idx: int, error=None) -> _Record:
    return _Record(idx, idx, idx + 1, {}, {}, errors=[], error=error)


def test_contiguous_prefix_stops_at_the_first_gap_or_failure():
    # What survives an interrupted or timed-out run is a gapless run of chunks
    # from the lowest one that finished. A scattered subset would leave the
    # caller a post-state no interrupted sequential run can produce.
    prefix = dispatch._contiguous_prefix
    assert prefix([]) == []
    assert [r.idx for r in prefix([_record(1), _record(2), _record(3)])] == [1, 2, 3]
    assert [r.idx for r in prefix([_record(1), _record(3)])] == [1]
    assert [r.idx for r in prefix([_record(4), _record(5)])] == [4, 5]
    assert [r.idx for r in prefix([_record(1), _record(2, ValueError()), _record(3)])] == [1]
    assert prefix([_record(1, ValueError())]) == []


def _stack_depth() -> int:
    depth = 0
    frame = sys._getframe(1)
    while frame is not None:
        depth += 1
        frame = frame.f_back
    return depth


def test_recursion_headroom_counts_the_live_stack_plus_the_frames_dispatch_adds():
    # The gate compares this against a fixed floor, so an off-by-one moves the
    # limit at which a user-lowered recursion limit stops being safe to
    # parallelize under.
    assert dispatch._recursion_headroom() == sys.getrecursionlimit() - _stack_depth() - 2


@pytest.mark.parametrize("span,expected", [((3, 4), 6400.0), ((2, 6), 1600.0)])
def test_chunk_probe_reports_nanoseconds_per_iteration(monkeypatch, span, expected):
    # A reduction cannot use the twin probe, so this is the only route into
    # _probe. The gate is fed a per-iteration cost; charging it the whole chunk
    # would scale the estimate with the chunk size. The spans start away from
    # zero so the width is not confusable with the bounds' sum.
    src = block(["total += xs[i]"], header="for i in range(len(xs)):")
    _, spec = build(src)
    env = {"xs": list(range(8)), "total": 0}
    plan = _plan_domain(spec.artifact, range(8))
    gate = preflight.check(spec, env, None)
    ticks = iter([1_000, 7_400])
    monkeypatch.setattr(time, "perf_counter_ns", lambda: next(ticks))
    record, per_iteration = dispatch._probe(spec, plan, span, env, None, gate)
    assert per_iteration == expected
    assert record.idx == 0
    assert record.error is None


def test_chunk_probe_captures_a_failure_instead_of_raising():
    # The probe runs real user code as chunk 0. Its exception belongs on the
    # record so the caller can commit the prefix before re-raising it.
    src = block(["total += 100 // xs[i]"], header="for i in range(len(xs)):")
    _, spec = build(src)
    env = {"xs": [0, 1, 2, 3], "total": 0}
    plan = _plan_domain(spec.artifact, range(4))
    gate = preflight.check(spec, env, None)
    record, _ = dispatch._probe(spec, plan, (0, 2), env, None, gate)
    assert isinstance(record.error, ZeroDivisionError)


def test_calibrated_reduction_commits_the_probe_chunk_exactly_once(monkeypatch):
    # The chunk probe leaves its contributions on a record instead of in env, so
    # the join has to fold it in; dropping or double-counting it moves the sum.
    src = block(["total += vals[i] * 1.0001"], header="for i in range(len(vals)):")
    _, spec = build(src)
    monkeypatch.setattr(dispatch, "_profitable", lambda *a, **k: True)
    env = {"vals": [0.1 * k + 0.007 for k in range(3000)], "total": 1.5}
    run_env = copy.deepcopy(env)
    result = execute(spec, range(3000), run_env, force_backend="thread")
    g = golden(src, env)
    assert run_env["total"] == g["total"]
    assert result == (2999, g["total"])
    stats = dispatch.get_block_stats()[spec.key]
    assert stats["probe_ns"] is not None
    assert stats["parallel_runs"] == 1


def test_interrupt_in_the_join_commits_only_the_completed_prefix(monkeypatch):
    # Ctrl-C in the collecting thread unwinds through _abandon: chunks that never
    # started are cancelled, running ones are settled before the caller's
    # containers are touched, and only the gapless prefix is committed.
    dispatch.shutdown()
    monkeypatch.setattr(dispatch.os, "cpu_count", lambda: 2)
    src = block(
        ["seen[i] = pace(xs[i])"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, backend=thread(pool_size=2, chunks=16), trust=callables",
    )
    _, spec = build(src)
    n = 160
    env = {"xs": list(range(n)), "seen": {}, "pace": lambda v: (time.sleep(0.01), v * 2)[1]}
    real_wait, calls = dispatch.wait, []

    def interrupting(fs, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            real_wait(fs, timeout=0.15)
            raise KeyboardInterrupt
        return real_wait(fs, **kwargs)

    monkeypatch.setattr(dispatch, "wait", interrupting)
    with pytest.raises(KeyboardInterrupt):
        execute(spec, range(n), env, force_backend="thread")

    committed = sorted(env["seen"])
    assert committed == list(range(len(committed)))
    assert 0 < len(committed) < n, "the run neither committed a prefix nor left work cancelled"
    assert all(env["seen"][i] == i * 2 for i in committed)
    assert dispatch.get_block_stats()[spec.key]["interrupted_runs"] == 1
