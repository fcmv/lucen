from __future__ import annotations

import ast
import copy
import random

import pytest

from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import select
from lucen.codegen import generate
from lucen.execution import dispatch
from lucen.execution.dispatch import execute, make_spec
from lucen.support import config
from lucen.support.errors import ErrorsMode, clear_fallback_report, set_errors_mode


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
    analysis = analyses[0]
    decision = select(analysis, workers=8)
    artifact = generate(analysis, decision, "t.py")
    assert artifact is not None, "expected a parallel-eligible block"
    return analysis, make_spec(analysis, decision, artifact)


def golden(src: str, env: dict) -> dict:
    g = copy.deepcopy(env)
    exec(src, g)
    return g


def iterable_of(analysis, env):
    it = analysis.for_node.iter
    if (
        analysis.for_node.iter
        and isinstance(it, ast.Call)
        and isinstance(it.func, ast.Name)
        and it.func.id == "enumerate"
    ):
        return eval(ast.unparse(it.args[0]), dict(env))
    return eval(ast.unparse(it), dict(env))


def run_backend(src, env, backend, chunks=None):
    analysis, spec = build(src)
    if chunks is not None:
        object.__setattr__(spec, "clauses", dict(spec.clauses))
    env = copy.deepcopy(env)
    result = execute(spec, iterable_of(analysis, env), env, force_backend=backend)
    return env, result


def block(body, header, clauses="calibrate=false"):
    body_src = "\n".join("    " + b for b in body)
    return f"# LUCEN START {clauses}\n{header}\n{body_src}\n# LUCEN END\n"


MAP_CASES = [
    (["ys[i] = xs[i] * 2 + 1"], "for i in range(len(xs)):"),
    (["ys[i] = xs[i] * xs[i] - 3"], "for i in range(len(xs)):"),
    (["t = xs[i] + 5", "ys[i] = t * t"], "for i in range(len(xs)):"),
    (["if xs[i] % 2 == 0:", "    ys[i] = xs[i]"], "for i in range(len(xs)):"),
    (
        [
            "if xs[i] > 0:",
            "    ys[i] = 1",
            "elif xs[i] < 0:",
            "    ys[i] = -1",
            "else:",
            "    ys[i] = 0",
        ],
        "for i in range(len(xs)):",
    ),
    (["ys[i] += xs[i]"], "for i in range(len(xs)):"),
    (["ys[i] = xs[i] // 2", "ys[i] += 1"], "for i in range(len(xs)):"),
]


@pytest.mark.parametrize("body,header", MAP_CASES)
@pytest.mark.parametrize("backend", ["thread", "process"])
@pytest.mark.parametrize("seed", range(4))
def test_map_equivalence(body, header, backend, seed):
    rng = random.Random(seed)
    n = rng.choice([1, 2, 7, 33, 64, 100, 257])
    xs = [rng.randint(-50, 50) for _ in range(n)]
    src = block(body, header)
    env = {"xs": xs, "ys": [0] * n}
    got, _ = run_backend(src, env, backend)
    assert got["ys"] == golden(src, env)["ys"]


REDUCTION_CASES = [
    (["total += xs[i]"], "for i in range(len(xs)):", "total", 0),
    (["total += xs[i] * 1.5"], "for i in range(len(xs)):", "total", 0.0),
    (["best = max(best, xs[i])"], "for i in range(len(xs)):", "best", -(10**9)),
    (["best = min(best, xs[i])"], "for i in range(len(xs)):", "best", 10**9),
    (["prod *= xs[i]"], "for i in range(len(xs)):", "prod", 1),
    (["if xs[i] > 0:", "    total += xs[i]"], "for i in range(len(xs)):", "total", 0),
    (["acc &= xs[i]"], "for i in range(len(xs)):", "acc", -1),
    (["acc |= xs[i]"], "for i in range(len(xs)):", "acc", 0),
    (["acc ^= xs[i]"], "for i in range(len(xs)):", "acc", 0),
]


@pytest.mark.parametrize("body,header,name,identity", REDUCTION_CASES)
@pytest.mark.parametrize("backend", ["thread", "process"])
@pytest.mark.parametrize("seed", range(4))
def test_reduction_equivalence(body, header, name, identity, backend, seed):
    rng = random.Random(seed + 100)
    n = rng.choice([1, 3, 16, 65, 128, 300])
    if isinstance(identity, float):
        xs = [rng.uniform(-5, 5) for _ in range(n)]
    else:
        xs = [rng.randint(1, 9) for _ in range(n)]
    src = block(body, header)
    env = {"xs": xs, name: identity}
    got, result = run_backend(src, env, backend)
    assert got[name] == golden(src, env)[name]


@pytest.mark.parametrize("seed", range(6))
def test_dag_wavefront_equivalence(seed):
    rng = random.Random(seed + 200)
    n = rng.choice([2, 8, 33, 100, 512, 1000])
    divisor = rng.choice([2, 3, 4])
    src = block(
        [f"out[i] = out[i // {divisor}] + w[i]"],
        "for i in range(1, n):",
        clauses="calibrate=false, grainsize=4",
    )
    out0 = rng.randint(1, 5)
    env = {"n": n, "out": [out0] + [0] * (n - 1), "w": [rng.randint(0, 9) for _ in range(n)]}
    got, _ = run_backend(src, env, "thread")
    assert got["out"] == golden(src, env)["out"]


@pytest.mark.parametrize("seed", range(4))
def test_enumerate_equivalence(seed):
    rng = random.Random(seed + 300)
    n = rng.choice([1, 5, 40, 129])
    items = [rng.randint(-9, 9) for _ in range(n)]
    src = block(["out[idx] = item * item + idx"], "for idx, item in enumerate(items):")
    env = {"items": items, "out": [0] * n}
    for backend in ("thread", "process"):
        got, _ = run_backend(src, copy.deepcopy(env), backend)
        assert got["out"] == golden(src, env)["out"]


@pytest.mark.parametrize("seed", range(4))
def test_dict_sequence_equivalence(seed):
    rng = random.Random(seed + 400)
    n = rng.choice([1, 4, 25, 90])
    keys = [f"k{rng.randint(0, 10_000)}_{j}" for j in range(n)]
    src = block(["cache[key] = len(key) + offset"], "for key in keys:")
    env = {"keys": keys, "cache": {}, "offset": rng.randint(0, 5)}
    for backend in ("thread", "process"):
        got, _ = run_backend(src, copy.deepcopy(env), backend)
        g = golden(src, env)
        assert got["cache"] == g["cache"]
        assert list(got["cache"].items()) == list(g["cache"].items())


def test_final_loop_variable_matches_sequential_across_backends():
    src = block(["ys[i] = xs[i]"], "for i in range(len(xs)):")
    env = {"xs": list(range(50)), "ys": [0] * 50}
    for backend in ("thread", "process"):
        _, result = run_backend(src, copy.deepcopy(env), backend)
        assert result == (49,)


def test_single_iteration_all_backends():
    src = block(["ys[i] = xs[i] * 7"], "for i in range(len(xs)):")
    env = {"xs": [11], "ys": [0]}
    for backend in ("thread", "process"):
        got, result = run_backend(src, copy.deepcopy(env), backend)
        assert got["ys"] == [77]
        assert result == (0,)
