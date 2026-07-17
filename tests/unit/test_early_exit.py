from __future__ import annotations

import ast
import copy
import time

import pytest

from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import Eligibility, select
from lucen.codegen import generate
from lucen.execution import dispatch
from lucen.execution.dispatch import execute, make_spec
from lucen.support import config
from lucen.support.errors import (
    ErrorsMode,
    clear_fallback_report,
    get_fallback_report,
    set_errors_mode,
)

EXP = frozenset({"early_exit"})


@pytest.fixture(autouse=True)
def _clean_state():
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config(experimental=EXP))
    yield
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config())


def build(src, experimental=EXP):
    scan = scan_source(src, "t.py")
    analysis = analyze_source(src, scan, "t.py")[0]
    decision = select(analysis, workers=8, experimental=experimental)
    artifact = generate(analysis, decision, "t.py")
    return analysis, decision, artifact


def run(src, env):
    analysis, decision, artifact = build(src)
    assert artifact is not None
    spec = make_spec(analysis, decision, artifact)
    env = copy.deepcopy(env)
    execute(spec, eval(ast.unparse(analysis.for_node.iter), dict(env)), env, force_backend="thread")
    return env, spec


def golden(src, env):
    g = copy.deepcopy(env)
    exec(src, g)
    return g


WRITE_THEN_BREAK = (
    "# LUCEN START calibrate=false\n"
    "for i in range(len(xs)):\n"
    "    if xs[i] < 0:\n"
    "        break\n"
    "    ys[i] = xs[i] * 2\n"
    "# LUCEN END\n"
)

BREAK_AFTER_WRITE = (
    "# LUCEN START calibrate=false\n"
    "for i in range(len(xs)):\n"
    "    ys[i] = xs[i] * 2\n"
    "    if xs[i] < 0:\n"
    "        break\n"
    "# LUCEN END\n"
)


def test_routes_to_early_exit_only_with_flag():
    _, decision, _ = build(WRITE_THEN_BREAK, experimental=frozenset())
    assert decision.eligibility is Eligibility.SEQUENTIAL
    _, decision2, _ = build(WRITE_THEN_BREAK, experimental=EXP)
    assert decision2.eligibility is Eligibility.EARLY_EXIT


@pytest.mark.parametrize("break_at", [0, 1, 137, 499, None])
def test_break_before_write_matches_sequential(break_at):
    xs = list(range(1, 501))
    if break_at is not None:
        xs[break_at] = -1
    env = {"xs": xs, "ys": [0] * 500}
    got, _ = run(WRITE_THEN_BREAK, env)
    assert got["ys"] == golden(WRITE_THEN_BREAK, env)["ys"]


@pytest.mark.parametrize("break_at", [0, 50, 300, 499])
def test_break_after_write_includes_exit_iteration(break_at):
    xs = list(range(1, 501))
    xs[break_at] = -1
    env = {"xs": xs, "ys": [0] * 500}
    got, _ = run(BREAK_AFTER_WRITE, env)
    g = golden(BREAK_AFTER_WRITE, env)
    assert got["ys"] == g["ys"]
    assert got["ys"][break_at] == -2


def test_no_break_runs_whole_range():
    xs = list(range(1, 301))
    env = {"xs": xs, "ys": [0] * 300}
    got, spec = run(WRITE_THEN_BREAK, env)
    assert got["ys"] == golden(WRITE_THEN_BREAK, env)["ys"]
    assert all(v != 0 for v in got["ys"])


def test_loop_variable_rebinds_to_exit_position():
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(len(xs)):\n"
        "    if xs[i] < 0:\n"
        "        break\n"
        "    ys[i] = i\n"
        "# LUCEN END\n"
    )
    xs = list(range(1, 401))
    xs[88] = -1
    env = {"xs": xs, "ys": [0] * 400}
    analysis, decision, artifact = build(src)
    spec = make_spec(analysis, decision, artifact)
    run_env = copy.deepcopy(env)
    result = execute(spec, range(400), run_env, force_backend="thread")
    assert result == (88,)


@pytest.mark.parametrize("seed", range(6))
def test_timing_perturbation_still_lowest_break_wins(seed):
    src = (
        "# LUCEN START calibrate=false, backend=thread(pool_size=4)\n"
        "for i in range(len(xs)):\n"
        "    if flags[i]:\n"
        "        break\n"
        "    ys[i] = slow(xs[i])\n"
        "# LUCEN END\n"
    )
    import random

    rng = random.Random(seed)
    n = 400
    first_break = rng.randint(10, n - 10)
    flags = [False] * n
    flags[first_break] = True
    flags[rng.randint(first_break + 1, n - 1)] = True
    env = {
        "xs": list(range(n)),
        "ys": [0] * n,
        "flags": flags,
        "slow": lambda v: (time.sleep(0.0002), v * 2)[1],
    }
    got, _ = run(src, env)
    g = golden(src, env)
    assert got["ys"] == g["ys"]
    assert got["ys"][first_break] == 0
    assert all(v == 0 for v in got["ys"][first_break:])


def test_return_still_sequential_even_with_flag():
    src = (
        "def f(xs, ys):\n"
        "    # LUCEN START calibrate=false\n"
        "    for i in range(len(xs)):\n"
        "        if xs[i] < 0:\n"
        "            return i\n"
        "        ys[i] = xs[i]\n"
        "    # LUCEN END\n"
    )
    scan = scan_source(src, "t.py")
    analysis = analyze_source(src, scan, "t.py")[0]
    decision = select(analysis, workers=8, experimental=EXP)
    assert decision.eligibility is Eligibility.SEQUENTIAL
    assert any(r.error == "EarlyExitRouting" for r in get_fallback_report())


def test_activate_rejects_unknown_experimental():
    import lucen

    with pytest.raises(ValueError):
        lucen.activate(experimental=["teleport"])
    lucen.deactivate()
