from __future__ import annotations

import ast
import copy
import logging

import pytest

from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import select
from lucen.codegen import generate
from lucen.execution import dispatch
from lucen.execution.dispatch import execute, make_spec
from lucen.support import config
from lucen.support.errors import (
    ErrorsMode,
    ParallelWriteConflictError,
    clear_fallback_report,
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


def build(src):
    scan = scan_source(src, "t.py")
    analysis = analyze_source(src, scan, "t.py")[0]
    decision = select(analysis, workers=8)
    artifact = generate(analysis, decision, "t.py")
    return analysis, make_spec(analysis, decision, artifact) if artifact else None


def run(src, env, backend="thread"):
    analysis, spec = build(src)
    assert spec is not None
    env = copy.deepcopy(env)
    it = analysis.for_node.iter
    if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "enumerate":
        iterable = eval(ast.unparse(it.args[0]), dict(env))
    else:
        iterable = eval(ast.unparse(it), dict(env))
    return env, execute(spec, iterable, env, force_backend=backend), spec


def golden(src, env):
    g = copy.deepcopy(env)
    exec(src, g)
    return g


def block(body, header="for i in range(len(xs)):", clauses="calibrate=false"):
    body_src = "\n".join("    " + b for b in body)
    return f"# LUCEN START {clauses}\n{header}\n{body_src}\n# LUCEN END\n"


DUP = "# LUCEN START calibrate=false\nfor key in keys:\n    seen[key] = key\n# LUCEN END\n"


def test_conflict_report_mode_reruns_and_logs_once(caplog):
    env = {"keys": ["a", "b", "c", "a", "d", "b", "e", "f"], "seen": {}}
    with caplog.at_level(logging.WARNING, logger="lucen"):
        got, _, spec = run(DUP, env)
    assert got["seen"] == golden(DUP, env)["seen"]
    conflicts = [r for r in caplog.records if "ParallelWriteConflict" in r.getMessage()]
    assert len(conflicts) == 1
    assert dispatch.get_block_stats()[spec.key]["fallback_runs"] == 1


def test_conflict_quiet_mode_silent(caplog):
    set_errors_mode("quiet")
    env = {"keys": ["a", "b", "a", "c"], "seen": {}}
    with caplog.at_level(logging.WARNING, logger="lucen"):
        got, _, _ = run(DUP, env)
    assert got["seen"] == golden(DUP, env)["seen"]
    assert caplog.records == []


def test_conflict_hard_mode_raises():
    set_errors_mode("hard")
    env = {"keys": ["a", "b", "a"], "seen": {}}
    analysis, spec = build(DUP)
    with pytest.raises(ParallelWriteConflictError):
        execute(spec, list(env["keys"]), copy.deepcopy(env), force_backend="thread")


def test_integer_index_conflict_caught_via_wired_audit():
    src = (
        "# LUCEN START calibrate=false, depend=none\n"
        "for i in range(len(idx)):\n    out[idx[i]] = i\n# LUCEN END\n"
    )
    idx = list(range(200)) + [50]
    env = {"idx": idx, "out": [-1] * 200}
    got, _, spec = run(src, env)
    assert got["out"] == golden(src, env)["out"]
    assert dispatch.get_block_stats()[spec.key]["fallback_runs"] == 1


def test_conflict_on_fallback_hard_overrides_report():
    src = (
        "# LUCEN START calibrate=false, on_fallback=hard\n"
        "for key in keys:\n    seen[key] = key\n# LUCEN END\n"
    )
    env = {"keys": ["a", "a"], "seen": {}}
    analysis, spec = build(src)
    with pytest.raises(ParallelWriteConflictError):
        execute(spec, list(env["keys"]), copy.deepcopy(env), force_backend="thread")


DIV = (
    "# LUCEN START calibrate=false{clauses}\n"
    "for i in range(len(xs)):\n    ys[i] = 100 // xs[i]\n# LUCEN END\n"
)


def test_fail_fast_default_raises_lowest_index():
    xs = [1] * 300
    xs[42] = 0
    xs[200] = 0
    env = {"xs": xs, "ys": [-1] * 300}
    analysis, spec = build(DIV.format(clauses=""))
    with pytest.raises(ZeroDivisionError):
        execute(spec, range(300), copy.deepcopy(env), force_backend="thread")


def test_collect_gathers_all_and_finishes():
    xs = [1] * 100
    for bad in (7, 30, 88):
        xs[bad] = 0
    env = {"xs": xs, "ys": [0] * 100}
    _, _, spec = run(DIV.format(clauses=", on_error=collect"), env)
    errors = dispatch.get_collected_errors(spec.key)
    assert sorted(i for i, _ in errors) == [7, 30, 88]


@pytest.mark.parametrize("backend", ["thread", "process"])
def test_fail_fast_prefix_matches_sequential(backend):
    xs = [1] * 200
    xs[150] = 0
    env = {"xs": xs, "ys": [-1] * 200}
    analysis, spec = build(DIV.format(clauses=""))
    run_env = copy.deepcopy(env)
    with pytest.raises(ZeroDivisionError):
        execute(spec, range(200), run_env, force_backend=backend)
    g = copy.deepcopy(env)
    try:
        exec(DIV.format(clauses=""), g)
    except ZeroDivisionError:
        pass
    assert run_env["ys"][:150] == g["ys"][:150]
    assert run_env["ys"][150] == -1


@pytest.mark.parametrize("grain", [1, 2, 8, 64])
def test_deep_dag_completes_across_grainsizes(grain):
    n = 5000
    src = (
        "# LUCEN START calibrate=false, grainsize={g}, "
        "backend=thread(pool_size=2)\n"
        "for i in range(1, n):\n    out[i] = out[i // 2] + w[i]\n"
        "# LUCEN END\n"
    ).format(g=grain)
    env = {"n": n, "out": [1] + [0] * (n - 1), "w": list(range(n))}
    got, _, _ = run(src, env)
    assert got["out"] == golden(src, env)["out"]


def test_monotonic_depth_chain_pool_two():
    src = "# LUCEN START\nfor i in range(1, n):\n    out[i] = out[i - 1] + 1\n# LUCEN END\n"
    analysis, spec = build(src)
    assert spec is None


def test_repeated_calls_reuse_pool_and_stay_correct():
    src = block(["ys[i] = big(xs[i])"], "for i in range(len(xs)):", clauses="calibrate=false")
    analysis, spec = build(src)
    base = {"xs": list(range(4000)), "ys": [0] * 4000, "big": lambda v: v * 3 + 1}
    for _ in range(5):
        env = copy.deepcopy(base)
        execute(spec, range(4000), env, force_backend="thread")
        assert env["ys"] == [v * 3 + 1 for v in range(4000)]
    stats = dispatch.get_block_stats()[spec.key]
    assert stats["runs"] == 5
    assert stats["parallel_runs"] == 5


def test_regime_change_invalidates_memo():
    src = block(["ys[i] = big(xs[i])"], "for i in range(len(xs)):")
    analysis, spec = build(src)
    small = {"xs": list(range(2000)), "ys": [0] * 2000, "big": lambda v: v + 1}
    execute(spec, range(2000), copy.deepcopy(small), force_backend="thread")
    huge = {"xs": list(range(50000)), "ys": [0] * 50000, "big": lambda v: v + 1}
    execute(spec, range(50000), copy.deepcopy(huge), force_backend="thread")
    assert dispatch.get_block_stats()[spec.key]["runs"] == 2


def test_hard_mode_scanner_error_still_unconditional():
    from lucen.support.errors import ClauseValueError

    set_errors_mode("hard")
    with pytest.raises(ClauseValueError):
        scan_source(block(["ys[i] = xs[i]"], clauses="backend=bogus"), "b.py")
    set_errors_mode("report")
    with pytest.raises(ClauseValueError):
        scan_source(block(["ys[i] = xs[i]"], clauses="backend=bogus"), "b.py")
