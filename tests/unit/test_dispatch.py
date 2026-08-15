from __future__ import annotations

import ast
import copy
import sys
import threading
import time

import pytest

from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import ClauseValue, scan_source
from lucen.analysis.selector import select
from lucen.codegen import generate
from lucen.execution import dispatch, nested_guard, preflight
from lucen.execution.dispatch import execute, make_spec
from lucen.execution.planning import _plan_domain, _Record
from lucen.support import config, costmodel
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
    assert any(
        r.error == "PreflightCheckError"
        and r.message.endswith(
            "could not start a worker for every chunk "
            "(RuntimeError: can't start new thread); ran SEQUENTIAL"
        )
        for r in get_fallback_report()
    )
    st = dispatch.get_block_stats()[spec.key]
    assert st["sequential_runs"] == 1
    assert st["fallback_runs"] == 1


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


def test_timeout_commits_the_finished_prefix_and_names_the_bound(monkeypatch):
    # The wait elapsing is what decides a timeout, not a re-read of the clock.
    # The chunks that did finish are committed as a prefix and the cancelled
    # tail is left exactly as the caller passed it in. The pool is pinned to two
    # threads so the tail is still queued, and so cancellable, on any host.
    dispatch.shutdown()
    monkeypatch.setattr(dispatch.os, "cpu_count", lambda: 2)
    src = block(
        ["seen[i] = crawl(xs[i])"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, backend=thread(pool_size=2, chunks=8), "
        "timeout=0.3, trust=callables",
    )
    _, spec = build(src)
    n = 16
    env = {
        "xs": list(range(n)),
        "seen": {},
        "crawl": lambda v: (time.sleep(0.001 if v < 4 else 0.5), v * 2)[1],
    }
    with pytest.raises(ParallelTimeoutError) as raised:
        execute(spec, range(n), env, force_backend="thread")
    assert raised.value.args[0].endswith(
        "block exceeded its timeout= bound (cooperative on THREAD: running chunks finished first)"
    )

    committed = sorted(env["seen"])
    assert 0 < len(committed) < n
    assert committed == list(range(len(committed)))
    assert all(env["seen"][i] == i * 2 for i in committed)
    assert dispatch.get_block_stats()[spec.key]["workers"] == 2


def test_a_block_that_beats_its_deadline_is_not_a_timeout():
    # The timeout branch is reached only when the wait elapsed; a run that
    # completed inside the bound must not be turned into a timeout by it.
    src = block(
        ["ys[i] = xs[i] * 2"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, backend=thread(chunks=4), timeout=30",
    )
    _, spec = build(src)
    n = 400
    env = {"xs": list(range(n)), "ys": [0] * n}
    execute(spec, range(n), env, force_backend="thread")
    assert env["ys"] == [v * 2 for v in range(n)]
    assert dispatch.get_block_stats()[spec.key]["parallel_runs"] == 1


def test_nested_guard_forces_sequential():
    src = block(["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):", clauses="calibrate=false")
    analysis, spec = build(src)
    env = {"xs": list(range(2000)), "ys": [0] * 2000}
    with nested_guard.dispatch_scope():
        execute(spec, range(2000), env, force_backend="thread")
    assert env["ys"] == golden(src, {"xs": env["xs"], "ys": [0] * 2000})["ys"]
    stats = dispatch.get_block_stats()[spec.key]
    assert stats["sequential_runs"] == 1
    assert any(
        r.error == "NestedParallelRegion"
        and r.message.endswith("nested parallel region: inner block runs SEQUENTIAL (spec 5.11)")
        for r in get_fallback_report()
    )


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
    assert any(
        r.error == "LimitClamp" and r.message.startswith("pool_size=")
        for r in get_fallback_report()
    )


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
    st = dispatch.get_block_stats()[spec.key]
    assert st["sequential_runs"] == 1
    assert st["fallback_runs"] == 1


_INSTRUMENTED = (
    "calibrate=false, timeout=5.0(per_task=true), "
    "progress=callback(cb, per_task=true), on_error=collect"
)


def test_generated_parameters_are_never_treated_as_user_names():
    # arg_names is what preflight resolves out of the caller's frame, so a
    # generated parameter leaking into it would be looked up as a user variable
    # and refuse the block on a name the user never wrote.
    src = block(["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):", clauses=_INSTRUMENTED)
    _, spec = build(src)
    assert dispatch._EXTRA_PARAMS <= set(spec.artifact.params)
    assert [n for n in spec.arg_names if n.startswith("_plx")] == []


def test_generated_parameters_resolve_to_their_runtime_values():
    src = block(["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):", clauses=_INSTRUMENTED)
    _, spec = build(src)
    env = {"xs": list(range(8)), "ys": [0] * 8, "cb": lambda *_: None}
    plan = _plan_domain(spec.artifact, range(8))
    record = dispatch._new_record(spec, plan, 1, 0, 4)
    gate = preflight.check(spec, env, None)

    bound = dict(
        zip(spec.artifact.params, dispatch._chunk_args(spec, plan, record, env, None, gate, 123.0))
    )
    assert bound["_plx_indices"] == range(0, 4)
    assert bound["_plx_errors"] is record.errors
    assert bound["_plx_clock"] is time.monotonic
    assert bound["_plx_deadline"] == 123.0
    assert isinstance(bound["_plx_timeout_error"], ParallelTimeoutError)
    assert bound["_plx_timeout_error"].args[0].endswith("per-iteration timeout= deadline exceeded")
    assert bound["_plx_progress"] is gate.progress_cb
    for slab_plan in spec.artifact.slabs:
        assert bound[slab_plan.param] is record.slabs[slab_plan.param]

    # no timeout= bound means the generated guard must never fire
    unbounded = dict(
        zip(spec.artifact.params, dispatch._chunk_args(spec, plan, record, env, None, gate, None))
    )
    assert unbounded["_plx_deadline"] == float("inf")


def test_free_threaded_reads_the_interpreter_probe(monkeypatch):
    monkeypatch.delattr(sys, "_is_gil_enabled", raising=False)
    assert dispatch.free_threaded() is False
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: True, raising=False)
    assert dispatch.free_threaded() is False
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: False, raising=False)
    assert dispatch.free_threaded() is True


def test_pool_threads_are_named_and_released_on_shutdown():
    dispatch.shutdown()
    pool = dispatch._ensure_pool(2)
    assert pool.submit(lambda: threading.current_thread().name).result().startswith("lucen")
    dispatch.shutdown()
    assert dispatch._pool is None


def test_shutdown_waits_for_work_already_running():
    # atexit runs this; returning before a chunk finishes would let the
    # interpreter tear down under a thread still writing into user containers.
    dispatch.shutdown()
    pool = dispatch._ensure_pool(2)
    finished: list = []
    pool.submit(lambda: (time.sleep(0.2), finished.append(1)))
    dispatch.shutdown()
    assert finished == [1]


def test_spec_repr_omits_the_compiled_pair():
    src = block(["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):", clauses="calibrate=false")
    _, spec = build(src)
    spec.fns()
    assert "_fns" not in repr(spec)


def test_calibration_memo_expires_by_use_count_and_regime_change():
    # The memo is what lets a hot block skip re-probing. It has to expire, or a
    # measurement taken at one size would keep routing a run of another size.
    src = block(["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):")
    _, spec = build(src)

    dispatch._memo[spec.key] = (500.0, 1000, 0)
    for _ in range(dispatch._MEMO_MAX_USES):
        assert dispatch._memo_lookup(spec, 1000) == 500.0
    assert dispatch._memo_lookup(spec, 1000) is None

    factor = dispatch._MEMO_REGIME_FACTOR
    dispatch._memo[spec.key] = (500.0, 1000, 0)
    assert dispatch._memo_lookup(spec, 1000 * factor) == 500.0
    assert dispatch._memo_lookup(spec, 1000 * factor + 1) is None
    assert dispatch._memo_lookup(spec, 1000 // factor) == 500.0
    assert dispatch._memo_lookup(spec, 1000 // factor - 1) is None


def _spec_with(clauses: str):
    src = block(["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):", clauses=clauses)
    return build(src)[1]


def test_on_fallback_override_resolves_every_clause_form():
    assert dispatch._fallback_override(_spec_with("calibrate=false"), "conflict") is None
    assert dispatch._fallback_override(_spec_with("on_fallback=hard"), "conflict") == "hard"
    assert dispatch._fallback_override(_spec_with("on_fallback=quiet"), "conflict") == "quiet"
    custom = _spec_with("on_fallback=custom(handler=cb)")
    assert dispatch._fallback_override(custom, "conflict") is None
    # an allowed reason is demoted to a report, everything else keeps the mode
    allowed = _spec_with("on_fallback=hard(allow=[unprofitable])")
    assert dispatch._fallback_override(allowed, "unprofitable") == "report"
    assert dispatch._fallback_override(allowed, "conflict") == "hard"


def test_sizing_honours_the_clause_then_the_config_then_the_ceiling(monkeypatch):
    explicit = _spec_with("calibrate=false, backend=thread(pool_size=3, chunks=7)")
    assert dispatch._sizing(explicit, 100, "thread") == (3, 7)
    # a domain smaller than the requested chunk count cannot be split that far
    assert dispatch._sizing(explicit, 4, "thread") == (3, 4)

    plain = _spec_with("calibrate=false")
    monkeypatch.setattr(dispatch.os, "cpu_count", lambda: 8)
    config.set_active(config.Config(max_threads_per_block=2, max_processes_per_block=5))
    assert dispatch._sizing(plain, 1000, "thread") == (2, 2 * costmodel.CHUNKS_PER_WORKER)
    assert dispatch._sizing(plain, 1000, "process") == (5, 5 * costmodel.PROCESS_CHUNKS_PER_WORKER)

    config.set_active(config.Config(defaults={"pool_size": 6, "chunks": 3}))
    assert dispatch._sizing(plain, 1000, "thread") == (6, 3)

    # an interpreter that cannot report its core count still has to size a pool
    config.set_active(config.Config())
    monkeypatch.setattr(dispatch.os, "cpu_count", lambda: None)
    assert dispatch._sizing(plain, 1000, "thread")[0] == 4


def test_profitability_weighs_measured_cost_against_dispatch_overhead():
    plain = _spec_with("calibrate=true")
    assert dispatch._profitable(plain, 10_000.0, 0, 8, 4, "thread") is False
    assert dispatch._profitable(plain, 10**9, 1, 8, 4, "thread") is True
    assert dispatch._profitable(plain, 10_000.0, 10_000, 8, 4, "thread") is True
    assert dispatch._profitable(plain, 0.001, 10_000, 8, 4, "thread") is False

    # a gain that only ties the overhead is not a gain
    overhead = costmodel.overhead_ns(4, 10_000, thread=True)
    tie = overhead / (10_000 * 0.5)
    assert dispatch._profitable(plain, tie, 10_000, 2, 4, "thread") is False
    assert dispatch._profitable(plain, tie * 1.001, 10_000, 2, 4, "thread") is True

    # threshold(min_gain=) scales the bar the projected gain has to clear
    strict = _spec_with("calibrate=threshold(min_gain=1000000.0)")
    assert dispatch._profitable(strict, 10_000.0, 10_000, 8, 4, "thread") is False


def test_block_stats_accumulate_across_runs():
    # get_block_stats is what --explain and the profile CLI read; every counter
    # is summed across runs, workers takes the max and backend the last set.
    src = block(
        ["ys[i] = xs[i] * 2"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, backend=thread(pool_size=2, chunks=4)",
    )
    _, spec = build(src)
    n = 400
    env = {"xs": list(range(n)), "ys": [0] * n}
    t_start = time.perf_counter_ns()
    for _ in range(2):
        execute(spec, range(n), copy.deepcopy(env), force_backend="thread")
    elapsed = time.perf_counter_ns() - t_start

    st = dispatch.get_block_stats()[spec.key]
    assert st["runs"] == 2
    assert st["parallel_runs"] == 2
    assert st["sequential_runs"] == 0
    assert st["chunks"] == 8
    assert st["workers"] == 2
    assert st["backend"] == "thread"
    assert st["fallback_runs"] == 0
    assert st["interrupted_runs"] == 0
    assert st["probe_ns"] is None
    assert 0 < st["duration_ns"] <= elapsed


def test_sequential_run_reports_no_workers_and_a_sequential_backend():
    src = block(["ys[i] = xs[i] * 2"], header="for i in range(len(xs)):", clauses="calibrate=false")
    _, spec = build(src)
    n = 50
    env = {"xs": list(range(n)), "ys": [0] * n}
    for _ in range(2):
        execute(spec, range(n), copy.deepcopy(env), force_backend="sequential")
    st = dispatch.get_block_stats()[spec.key]
    assert st["sequential_runs"] == 2
    assert st["parallel_runs"] == 0
    assert st["chunks"] == 0
    assert st["workers"] == 0
    assert st["backend"] == "sequential"


@pytest.mark.parametrize("n", [1, 200])
def test_probe_that_consumes_the_domain_still_rebinds_the_loop_target(monkeypatch, n):
    # With a single chunk the probe runs every iteration, so the twin that
    # follows it has nothing left and hands back the SKIP marker instead of a
    # loop target. The rebind then has to come from the plan. n=1 is the
    # boundary: the probe consumed one iteration and start is exactly 1.
    src = block(
        ["ys[i] = xs[i] + 1"],
        header="for i in range(len(xs)):",
        clauses="backend=thread(chunks=1)",
    )
    _, spec = build(src)
    monkeypatch.setattr(dispatch, "_profitable", lambda *a, **k: False)
    env = {"xs": list(range(n)), "ys": [0] * n}
    result = execute(spec, range(n), env, force_backend="thread")
    assert env["ys"] == [v + 1 for v in range(n)]
    assert result == (n - 1,)


def test_sequential_reduction_rebinds_every_accumulator():
    # The twin returns the loop targets first and then one value per rebindable
    # reduction, so the offset into that tuple decides which name gets which.
    src = block(
        ["total += vals[i]", "count += 1"],
        header="for i in range(len(vals)):",
        clauses="calibrate=false",
    )
    _, spec = build(src)
    env = {"vals": [1.5] * 100, "total": 2.0, "count": 0}
    result = execute(spec, range(100), env, force_backend="sequential")
    assert env["total"] == 152.0
    assert env["count"] == 100
    # loop target first, then one value per rebindable reduction in artifact order
    assert result == (99, 100, 152.0)


def test_buffer_direct_after_a_probe_skips_the_chunk_already_written(monkeypatch):
    # The direct path writes into the caller's buffer, so the chunk the probe
    # already wrote must be dropped from the work list rather than written twice.
    src = block(
        ["ys[i] = xs[i] * 3"],
        header="for i in range(len(xs)):",
        clauses="backend=thread(pool_size=2, chunks=4)",
    )
    _, spec = build(src)
    assert spec.artifact.buffer_fast_path, "this block no longer takes the direct path"
    monkeypatch.setattr(dispatch, "_profitable", lambda *a, **k: True)
    n = 4000
    env = {"xs": list(range(n)), "ys": [0] * n}
    result = execute(spec, range(n), env, force_backend="thread")
    assert env["ys"] == [v * 3 for v in range(n)]
    assert result == (n - 1,)
    st = dispatch.get_block_stats()[spec.key]
    assert st["parallel_runs"] == 1
    assert st["chunks"] == 3
    assert st["workers"] == 2


def test_recursion_headroom_exactly_at_the_floor_still_parallelizes(monkeypatch):
    # The floor is the smallest headroom dispatch will run under, not the
    # largest it refuses at.
    src = block(["ys[i] = xs[i] * 2"], header="for i in range(len(xs)):", clauses="calibrate=false")
    _, spec = build(src)
    monkeypatch.setattr(dispatch, "_recursion_headroom", lambda: dispatch._MIN_RECURSION_HEADROOM)
    n = 400
    env = {"xs": list(range(n)), "ys": [0] * n}
    execute(spec, range(n), env, force_backend="thread")
    assert env["ys"] == [v * 2 for v in range(n)]
    st = dispatch.get_block_stats()[spec.key]
    assert st["parallel_runs"] == 1
    assert not any(r.error == "RecursionHeadroom" for r in get_fallback_report())


def test_low_recursion_headroom_names_the_floor_it_wants():
    src = block(["ys[i] = xs[i] * 2"], header="for i in range(len(xs)):", clauses="calibrate=false")
    _, spec = build(src)
    env = {"xs": list(range(200)), "ys": [0] * 200}
    limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(60)
        execute(spec, range(200), env, force_backend="thread")
    finally:
        sys.setrecursionlimit(limit)
    assert any(
        r.error == "RecursionHeadroom"
        and r.message.endswith(
            f"recursion headroom below {dispatch._MIN_RECURSION_HEADROOM} frames "
            "(sys.setrecursionlimit); parallel machinery needs more, ran SEQUENTIAL"
        )
        for r in get_fallback_report()
    )


def test_calibrate_always_probes_even_a_block_it_has_measured():
    # auto consults the memo first; always is the mode that re-measures, so it
    # has to reach the probe on every run.
    src = block(
        ["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):", clauses="calibrate=always"
    )
    _, spec = build(src)
    n = 300
    env = {"xs": list(range(n)), "ys": [0] * n}
    execute(spec, range(n), env, force_backend="thread")
    assert env["ys"] == [v + 1 for v in range(n)]
    assert dispatch.get_block_stats()[spec.key]["probe_ns"] is not None


def test_a_calibrate_false_default_overrides_the_static_prediction():
    # The literal clause skips the static screen during selection, but a default
    # from lucen.toml does not: the block is still marked unprofitable and the
    # mode is what has to override it at dispatch.
    src = block(["ys[i] = xs[i] + 1"], header="for i in range(20):")
    _, spec = build(src)
    assert spec.static_unprofitable, "this block is no longer statically screened out"
    config.set_active(config.Config(defaults={"calibrate": "false"}))
    # selection already reported the static prediction; only a second report
    # from dispatch would mean the override did not take
    clear_fallback_report()
    env = {"xs": list(range(20)), "ys": [0] * 20}
    execute(spec, range(20), env, force_backend="thread")
    assert env["ys"] == [v + 1 for v in range(20)]
    st = dispatch.get_block_stats()[spec.key]
    assert st["parallel_runs"] == 1
    assert not any(r.error == "PARALLEL_UNPROFITABLE" for r in get_fallback_report())


def test_ft_promotion_takes_the_threshold_inclusively(monkeypatch):
    _, spec = build(block(["ys[i] = big(xs[i])"], header="for i in range(len(xs)):"))
    n = 2000
    monkeypatch.setattr(dispatch, "free_threaded", lambda: True)
    dispatch._memo[spec.key] = (float(costmodel.FT_THREAD_MIN_NS), n, 0)
    env = {"xs": list(range(n)), "ys": [0] * n, "big": lambda v: v * 2 + 1}
    execute(spec, range(n), env)
    assert env["ys"] == [v * 2 + 1 for v in range(n)]
    assert dispatch.get_block_stats()[spec.key]["backend"] == "thread"


def test_dict_audit_still_fires_when_a_list_slab_shares_the_block():
    # The audit walks every slab plan and skips the non-dict ones. A block that
    # writes both kinds must still have its dict half audited.
    src = block(
        ["ys[i] = i * 2", "seen[key] = i"],
        header="for i, key in enumerate(keys):",
        clauses="calibrate=false",
    )
    _, spec = build(src)
    kinds = [sp.kind for sp in spec.artifact.slabs]
    assert "list" in kinds and "dict" in kinds
    env = {"keys": ["a", "b", "c", "d", "a", "e", "f", "g"], "ys": [0] * 8, "seen": {}}
    got, _, _ = run(src, env)
    assert got["seen"] == golden(src, env)["seen"]
    assert any(r.error == "ParallelWriteConflictError" for r in get_fallback_report())


def test_write_conflict_report_names_the_container_and_key():
    src = block(["seen[key] = key * 2"], header="for key in keys:", clauses="calibrate=false")
    env = {"keys": ["a", "b", "c", "d", "a", "e", "f", "g"], "seen": {}}
    run(src, env)
    assert any(
        r.error == "ParallelWriteConflictError"
        and r.message.endswith(
            "chunks wrote 'seen['a']' more than once; discarding the parallel "
            "attempt and re-running sequentially"
        )
        for r in get_fallback_report()
    )


def test_dict_slab_run_reports_its_chunks_through_the_join():
    # The dict slab keeps the block off the buffer-direct path, so this is the
    # accounting the join itself does.
    src = block(
        ["seen[i] = xs[i] * 2"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, backend=thread(chunks=4)",
    )
    _, spec = build(src)
    n = 400
    env = {"xs": list(range(n)), "seen": {}}
    for _ in range(2):
        execute(spec, range(n), copy.deepcopy(env), force_backend="thread")
    st = dispatch.get_block_stats()[spec.key]
    assert st["parallel_runs"] == 2
    assert st["chunks"] == 8
    assert st["fallback_runs"] == 0


def test_arg_names_root_dotted_targets_and_exclude_generated_ones():
    # arg_names is the set preflight resolves from the caller's frame, so an
    # attribute target contributes its root object and nothing generated leaks.
    src = block(
        ["acc.total += obj.buf[i]"],
        header="for i in range(len(obj.buf)):",
        clauses="calibrate=false",
    )
    _, spec = build(src)
    assert "obj" in spec.arg_names
    assert "acc" in spec.arg_names
    assert not [n for n in spec.arg_names if "." in n or n.startswith("_plx")]


def test_arg_names_keep_the_sequence_domain_parameters_out():
    # a non-range domain adds _plx_seq to the chunk signature; it is generated,
    # so it must not join the names preflight resolves from the caller
    src = block(["ys[i] = i"], header="for i, v in enumerate(items):", clauses="calibrate=false")
    _, spec = build(src)
    assert "_plx_seq" in spec.artifact.params
    assert spec.arg_names == ("ys",)


def test_grainsize_comes_from_the_clause_or_falls_back_to_the_default():
    assert _spec_with("calibrate=false").grainsize == 1024
    assert _spec_with("calibrate=false, grainsize=8").grainsize == 8


def test_a_record_is_sized_by_the_width_of_its_chunk():
    src = block(
        ["ys[i] = xs[i]", "total += xs[i]"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false",
    )
    _, spec = build(src)
    plan = _plan_domain(spec.artifact, range(10))
    record = dispatch._new_record(spec, plan, 1, 2, 6)
    assert record.slabs and record.sites
    assert all(len(slab) == 4 for slab in record.slabs.values())
    assert all(len(site) == 4 for site in record.sites.values())


@pytest.mark.parametrize("span,expected", [((3, 4), 6400.0), ((2, 6), 1600.0)])
def test_twin_probe_also_reports_nanoseconds_per_iteration(monkeypatch, span, expected):
    src = block(["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):")
    _, spec = build(src)
    assert dispatch._twin_probe_ok(spec)
    env = {"xs": list(range(8)), "ys": [0] * 8}
    plan = _plan_domain(spec.artifact, range(8))
    ticks = iter([1_000, 7_400])
    monkeypatch.setattr(time, "perf_counter_ns", lambda: next(ticks))
    assert dispatch._probe_twin(spec, plan, span, env, None) == expected


def test_unprofitable_reports_name_whether_the_cost_was_measured():
    spec = _spec_with("calibrate=false")
    dispatch._handle_unprofitable(spec, None)
    dispatch._handle_unprofitable(spec, 42.4)
    messages = [r.message for r in get_fallback_report() if r.error == "PARALLEL_UNPROFITABLE"]
    assert any(
        m.endswith(
            "statically predicted to lose to dispatch overhead; ran SEQUENTIAL "
            "(calibrate=false overrides, spec 5.17)"
        )
        for m in messages
    )
    assert any(
        m.endswith(
            "measured ~42 ns/iteration loses to dispatch overhead; ran SEQUENTIAL "
            "(calibrate=false overrides, spec 5.17)"
        )
        for m in messages
    )


def test_strict_and_on_fallback_promote_an_unprofitable_block_to_an_error():
    from lucen.support.errors import UnprofitableParallelismError

    with pytest.raises(UnprofitableParallelismError):
        dispatch._handle_unprofitable(_spec_with("calibrate=false, strict=true"), 10.0)
    with pytest.raises(UnprofitableParallelismError):
        dispatch._handle_unprofitable(_spec_with("calibrate=false, on_fallback=hard"), 10.0)
    # both gates name the reason, so allowing it keeps the block on the report
    # path rather than promoting it
    dispatch._handle_unprofitable(
        _spec_with("calibrate=false, strict=true(allow=[unprofitable])"), 10.0
    )
    dispatch._handle_unprofitable(
        _spec_with("calibrate=false, on_fallback=hard(allow=[unprofitable])"), 10.0
    )
    assert any(r.error == "PARALLEL_UNPROFITABLE" for r in get_fallback_report())


def test_progress_true_prints_each_chunk_to_stderr(capsys):
    src = block(
        ["ys[i] = xs[i] + 1"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, progress=true, backend=thread(chunks=4)",
    )
    _, spec = build(src)
    n = 400
    execute(spec, range(n), {"xs": list(range(n)), "ys": [0] * n}, force_backend="thread")
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("lucen: ")]
    assert lines == [f"lucen: t.py:1: {k}/{n} iterations" for k in (100, 200, 300, 400)]


def test_progress_false_prints_nothing(capsys):
    src = block(
        ["ys[i] = xs[i] + 1"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, progress=false, backend=thread(chunks=4)",
    )
    _, spec = build(src)
    n = 400
    execute(spec, range(n), {"xs": list(range(n)), "ys": [0] * n}, force_backend="thread")
    assert "iterations" not in capsys.readouterr().err


def test_timeout_clamps_only_above_the_ceiling():
    config.set_active(config.Config(max_timeout_seconds=5.0))
    at_ceiling = _spec_with("calibrate=false, timeout=5.0")
    before = time.monotonic()
    assert dispatch._deadline(at_ceiling) >= before + 5.0
    assert not any(r.error == "LimitClamp" for r in get_fallback_report())

    dispatch._deadline(_spec_with("calibrate=false, timeout=30"))
    assert any(
        r.error == "LimitClamp" and r.message.startswith("timeout=") for r in get_fallback_report()
    )


def test_calibrate_true_still_measures():
    src = block(["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):", clauses="calibrate=true")
    _, spec = build(src)
    n = 300
    execute(spec, range(n), {"xs": list(range(n)), "ys": [0] * n}, force_backend="thread")
    assert dispatch.get_block_stats()[spec.key]["probe_ns"] is not None


def _collecting_block(max_errors: int, zeros) -> tuple:
    src = block(
        ["ys[i] = 100 // xs[i]"],
        header="for i in range(len(xs)):",
        clauses=(
            f"calibrate=false, backend=thread(chunks=4), on_error=collect(max_errors={max_errors})"
        ),
    )
    _, spec = build(src)
    n = 200
    xs = [1] * n
    for z in zeros:
        xs[z] = 0
    execute(spec, range(n), {"xs": xs, "ys": [-1] * n}, force_backend="thread")
    return spec, dispatch.get_collected_errors(spec.key)


def test_max_errors_reports_only_once_the_budget_is_passed():
    # The budget is a ceiling on collected errors, not a target: hitting it
    # exactly is still within the contract the block asked for.
    _, at_budget = _collecting_block(2, (13, 150))
    assert len(at_budget) == 2
    assert not any(r.error == "MaxErrorsExceeded" for r in get_fallback_report())

    clear_fallback_report()
    _, over_budget = _collecting_block(2, (13, 90, 150))
    assert len(over_budget) == 3
    assert any(
        r.error == "MaxErrorsExceeded"
        and r.message.endswith("on_error collect exceeded max_errors=2")
        for r in get_fallback_report()
    )


def test_chunk_errors_propagate_in_iteration_order():
    # A dict slab keeps the block off the buffer-direct path, so this is the
    # chunk-set join deciding which failure the caller sees: the earliest one by
    # index, with everything before it committed. The later chunk fails a
    # different way, so the raised error identifies the record the join picked.
    #
    # The two failures are ordered by an event rather than by where they sit in
    # their chunks: the later chunk fails on its second iteration and the
    # earlier one on its thirty-eighth, so left to the scheduler the later
    # failure can return the wait first and cancel the earlier chunk.
    src = block(
        ["seen[i] = divide(xs[i])"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, backend=thread(chunks=4), trust=callables",
    )
    _, spec = build(src)
    n = 400
    earlier_failed = threading.Event()

    def divide(v):
        if v == 137:
            earlier_failed.set()
            raise ZeroDivisionError("earlier chunk")
        if v == 301:
            assert earlier_failed.wait(5), "the earlier chunk never failed"
            raise TypeError("later chunk")
        return 100

    env = {"xs": list(range(n)), "seen": {}, "divide": divide}
    with pytest.raises(ZeroDivisionError):
        execute(spec, range(n), env, force_backend="thread")
    committed = sorted(env["seen"])
    assert committed == list(range(137))
    assert all(env["seen"][i] == 100 for i in committed)


def test_buffer_direct_sizes_a_pool_without_a_core_count(monkeypatch):
    dispatch.shutdown()
    monkeypatch.setattr(dispatch.os, "cpu_count", lambda: None)
    src = block(
        ["ys[i] = xs[i] * 3"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, backend=thread(pool_size=2, chunks=4)",
    )
    _, spec = build(src)
    assert spec.artifact.buffer_fast_path
    n = 400
    env = {"xs": list(range(n)), "ys": [0] * n}
    execute(spec, range(n), env, force_backend="thread")
    assert env["ys"] == [v * 3 for v in range(n)]
    assert dispatch._pool._max_workers == 4


def test_bounds_tile_the_domain_without_a_gap_or_an_overlap():
    from lucen.execution.planning import _bounds

    assert _bounds(10, 1) == [(0, 10)]
    assert _bounds(10, 4) == [(0, 3), (3, 6), (6, 9), (9, 10)]
    assert _bounds(0, 4) == []


def test_a_fresh_record_carries_no_error_and_no_exit():
    record = _Record(1, 0, 4, {}, {}, errors=[])
    assert record.error is None
    assert record.exit_pos is None


@pytest.mark.parametrize("name", ["thread", "process", "sequential"])
def test_an_explicit_backend_clause_is_recognised(name):
    # backend=sequential leaves the block unparallelized, so there is no
    # artifact to build one from; the clause is substituted instead.
    spec = _spec_with("calibrate=false, backend=thread")
    spec.clauses["backend"] = ClauseValue(raw=name, kind="name", value=name)
    assert dispatch._explicit_backend(spec) == name
    assert dispatch._pick_backend(spec) == name


def test_an_unknown_backend_name_is_not_treated_as_explicit():
    spec = _spec_with("calibrate=false, backend=thread")
    spec.clauses["backend"] = ClauseValue(raw="teleport", kind="name", value="teleport")
    assert dispatch._explicit_backend(spec) is None


def test_twin_probe_is_refused_for_a_dict_slab():
    # The twin writes straight into env, which a dict slab cannot undo, so the
    # chunk probe is the only safe one for it.
    src = block(["seen[key] = key"], header="for key in keys:", clauses="calibrate=false")
    _, spec = build(src)
    assert dispatch._twin_probe_ok(spec) is False


def test_a_name_is_read_from_env_then_module_globals_then_builtins():
    assert dispatch._value_of("k", {"k": 1}, {"k": 2}) == 1
    assert dispatch._value_of("k", {}, {"k": 2}) == 2
    assert dispatch._value_of("len", {}, {}) is len


def test_arg_names_root_a_dotted_slab_container():
    src = block(
        ["obj.buf[i] = xs[i] * 2"], header="for i in range(len(xs)):", clauses="calibrate=false"
    )
    _, spec = build(src)
    assert [p.container for p in spec.artifact.slabs] == ["obj.buf"]
    assert spec.arg_names == ("obj", "xs")


def test_wavefront_process_default_explains_why_it_ran_sequentially(monkeypatch):
    monkeypatch.setattr(dispatch, "free_threaded", lambda: False)
    src = block(["out[i] = out[i // 2] + w[i]"], clauses="calibrate=false")
    _, spec = build(src)
    env = {"n": 2048, "out": [1] + [0] * 2047, "w": list(range(2048))}
    execute(spec, range(1, 2048), env, force_backend=None)
    assert any(
        r.error == "WavefrontSequentialDefault"
        and r.message.endswith(
            "recognized-DAG wavefront runs SEQUENTIAL by default (PROCESS per-level "
            "dispatch is slower; force backend=thread to run the wavefront on a "
            "free-threaded build, spec 5.6)"
        )
        for r in get_fallback_report()
    )


def test_a_wavefront_that_refuses_mid_run_falls_back_and_is_counted(monkeypatch):
    # The wavefront can refuse after dispatch has begun; the block then has to
    # re-run sequentially, surface the reason it refused, and count one fallback.
    from lucen.execution import wavefront
    from lucen.support.errors import PreflightCheckError

    src = block(
        ["out[i] = out[i // 2] + w[i]"], clauses="calibrate=false, backend=thread, grainsize=8"
    )
    _, spec = build(src)

    def refuse(*_args, **_kwargs):
        raise PreflightCheckError("wavefront refused mid run", file="t.py", line=1)

    monkeypatch.setattr(wavefront, "execute_wavefront", refuse)
    n = 2048
    env = {"n": n, "out": [1] + [0] * (n - 1), "w": list(range(n))}
    execute(spec, range(1, n), env, force_backend="thread")

    golden_env = {"n": n, "out": [1] + [0] * (n - 1), "w": list(range(n))}
    exec(src, golden_env)
    assert env["out"] == golden_env["out"]
    st = dispatch.get_block_stats()[spec.key]
    assert st["sequential_runs"] == 1
    assert st["fallback_runs"] == 1
    assert any(
        r.error == "PreflightCheckError" and r.message.endswith("wavefront refused mid run")
        for r in get_fallback_report()
    )


def test_a_chunk_set_that_cannot_start_its_workers_is_counted_once(monkeypatch):
    # A dict slab keeps the block off the buffer-direct path, so this is the
    # chunk-set half of the same worker-exhaustion fallback.
    dispatch.shutdown()
    monkeypatch.setattr(dispatch.os, "cpu_count", lambda: None)
    src = block(
        ["seen[i] = xs[i] * 2"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, backend=thread(chunks=4)",
    )
    _, spec = build(src)
    n = 400
    pool = dispatch._ensure_pool(4)
    assert pool._max_workers == 4
    original, calls = pool.submit, []

    def exhausted(*args, **kwargs):
        calls.append(1)
        if len(calls) > 2:
            raise RuntimeError("can't start new thread")
        return original(*args, **kwargs)

    pool.submit = exhausted
    env = {"xs": list(range(n)), "seen": {}}
    try:
        execute(spec, range(n), env, force_backend="thread")
    finally:
        pool.submit = original
    assert env["seen"] == {i: i * 2 for i in range(n)}
    st = dispatch.get_block_stats()[spec.key]
    assert st["sequential_runs"] == 1
    assert st["fallback_runs"] == 1


def test_a_failure_cancels_the_queued_tail_and_commits_the_prefix(monkeypatch):
    # FIRST_EXCEPTION returns while chunks are still queued. Those are cancelled
    # and the ones already running are settled before anything is committed, so
    # the caller still sees a gapless prefix.
    dispatch.shutdown()
    monkeypatch.setattr(dispatch.os, "cpu_count", lambda: 2)
    src = block(
        ["seen[i] = pace(xs[i])"],
        header="for i in range(len(xs)):",
        clauses="calibrate=false, backend=thread(pool_size=2, chunks=16), trust=callables",
    )
    _, spec = build(src)
    n = 160
    ran: list = []

    def pace(v):
        ran.append(v)
        if v == 3:
            raise ZeroDivisionError("chunk one")
        if v >= 10:
            time.sleep(0.02)
        return v * 2

    env = {"xs": list(range(n)), "seen": {}, "pace": pace}
    with pytest.raises(ZeroDivisionError):
        execute(spec, range(n), env, force_backend="thread")

    assert len(ran) < n, "nothing was cancelled, so the queued tail never existed"
    # the failing chunk is the first, so the committed prefix is exactly the
    # iterations it completed before raising
    assert sorted(env["seen"]) == [0, 1, 2]
    assert all(env["seen"][i] == i * 2 for i in env["seen"])


def test_probe_measures_only_what_is_left_to_run(monkeypatch):
    # The gate is asked about the remaining iterations, not the whole domain;
    # counting the probed chunk twice would inflate the projected gain.
    src = block(["ys[i] = xs[i] + 1"], header="for i in range(len(xs)):")
    _, spec = build(src)
    seen: list = []

    def record_remaining(spec_, t_ns, remaining, workers, n_chunks, backend):
        seen.append(remaining)
        return False

    monkeypatch.setattr(dispatch, "_profitable", record_remaining)
    n = 400
    env = {"xs": list(range(n)), "ys": [0] * n}
    execute(spec, range(n), env, force_backend="thread")
    assert seen, "the gate was never consulted"
    probed = dispatch.get_block_stats()[spec.key]["chunks"]
    assert seen[0] < n, f"remaining {seen[0]} is not smaller than the domain {n}"
    assert env["ys"] == [v + 1 for v in range(n)]
    assert probed == 0


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
    st = dispatch.get_block_stats()[spec.key]
    assert st["interrupted_runs"] == 1
    assert st["workers"] == 2
