from __future__ import annotations

import ast
import copy
import math

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

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


def bit_equal(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return repr(a) == repr(b)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(bit_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return list(a.keys()) == list(b.keys()) and all(bit_equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _block(body_lines, header, clauses="calibrate=false"):
    body = "\n".join("    " + line for line in body_lines)
    return f"# LUCEN START {clauses}\n{header}\n{body}\n# LUCEN END\n"


def _build(src, workers=8):
    scan = scan_source(src, "t.py")
    analysis = analyze_source(src, scan, "t.py")[0]
    decision = select(analysis, workers=workers)
    artifact = generate(analysis, decision, "t.py")
    if artifact is None:
        return None, None
    return analysis, make_spec(analysis, decision, artifact)


def _iterable_of(analysis, env):
    it = analysis.for_node.iter
    if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "enumerate":
        return eval(ast.unparse(it.args[0]), dict(env))
    return eval(ast.unparse(it), dict(env))


def _golden(src, env):
    g = copy.deepcopy(env)
    exec(src, g)
    return g


def _run(src, env, backend, workers=8):
    analysis, spec = _build(src, workers)
    assume(spec is not None)
    run_env = copy.deepcopy(env)
    execute(spec, _iterable_of(analysis, run_env), run_env, force_backend=backend)
    return run_env


_ATOMS = st.sampled_from(["xs[i]", "i"])
_SMALL_INT = st.integers(min_value=-6, max_value=6)


def _expr(depth):
    atom = st.one_of(_ATOMS, _SMALL_INT.map(str))
    if depth <= 0:
        return atom
    op = st.sampled_from([" + ", " - ", " * "])
    sub = _expr(depth - 1)
    return st.one_of(
        atom,
        st.builds(lambda a, o, b: f"({a}{o}{b})", sub, op, sub),
    )


EXPR = _expr(3)
SIZES = st.integers(min_value=0, max_value=260)
BACKENDS = st.sampled_from(["thread", "process"])


@given(expr=EXPR, n=SIZES, backend=BACKENDS, data=st.data())
def test_map_is_bit_identical(expr, n, backend, data):
    xs = data.draw(st.lists(st.integers(min_value=-1000, max_value=1000), min_size=n, max_size=n))
    src = _block([f"ys[i] = {expr}"], "for i in range(len(xs)):")
    env = {"xs": xs, "ys": [0] * n}
    got = _run(src, env, backend)
    assert bit_equal(got["ys"], _golden(src, env)["ys"])


@given(expr=EXPR, n=st.integers(min_value=0, max_value=200), backend=BACKENDS, data=st.data())
def test_float_map_is_bit_identical(expr, n, backend, data):
    xs = data.draw(
        st.lists(
            st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6),
            min_size=n,
            max_size=n,
        )
    )
    src = _block([f"ys[i] = ({expr}) * 1.5 + xs[i]"], "for i in range(len(xs)):")
    env = {"xs": xs, "ys": [0.0] * n}
    got = _run(src, env, backend)
    assert bit_equal(got["ys"], _golden(src, env)["ys"])


@given(n=st.integers(min_value=0, max_value=300), backend=BACKENDS, data=st.data())
def test_float_sum_reduction_is_bit_identical(n, backend, data):
    xs = data.draw(
        st.lists(
            st.floats(allow_nan=False, allow_infinity=False, min_value=-1e8, max_value=1e8),
            min_size=n,
            max_size=n,
        )
    )
    src = _block(["total += xs[i]"], "for i in range(len(xs)):")
    env = {"xs": xs, "total": 0.0}
    got = _run(src, env, backend)
    assert bit_equal(got["total"], _golden(src, env)["total"])


@given(n=st.integers(min_value=1, max_value=200), backend=BACKENDS, data=st.data())
def test_bignum_product_reduction_is_exact(n, backend, data):
    xs = data.draw(st.lists(st.integers(min_value=-30, max_value=30), min_size=n, max_size=n))
    src = _block(["prod *= xs[i]"], "for i in range(len(xs)):")
    env = {"xs": xs, "prod": 1}
    got = _run(src, env, backend)
    assert got["prod"] == _golden(src, env)["prod"]


@given(
    n=st.integers(min_value=1, max_value=250),
    op=st.sampled_from(["min", "max"]),
    backend=BACKENDS,
    data=st.data(),
)
def test_minmax_reduction_matches_sequential(n, op, backend, data):
    xs = data.draw(
        st.lists(st.integers(min_value=-(10**9), max_value=10**9), min_size=n, max_size=n)
    )
    identity = 10**18 if op == "min" else -(10**18)
    src = _block([f"best = {op}(best, xs[i])"], "for i in range(len(xs)):")
    env = {"xs": xs, "best": identity}
    got = _run(src, env, backend)
    assert got["best"] == _golden(src, env)["best"]


@given(
    n=st.integers(min_value=1, max_value=250),
    op=st.sampled_from(["&", "|", "^"]),
    backend=BACKENDS,
    data=st.data(),
)
def test_bitwise_reduction_matches_sequential(n, op, backend, data):
    xs = data.draw(st.lists(st.integers(min_value=0, max_value=2**32), min_size=n, max_size=n))
    identity = -1 if op == "&" else 0
    src = _block([f"acc {op}= xs[i]"], "for i in range(len(xs)):")
    env = {"xs": xs, "acc": identity}
    got = _run(src, env, backend)
    assert got["acc"] == _golden(src, env)["acc"]


@given(
    n=st.integers(min_value=2, max_value=1200),
    c=st.integers(min_value=2, max_value=5),
    grain=st.sampled_from([1, 2, 4, 16]),
    data=st.data(),
)
def test_dag_wavefront_matches_sequential(n, c, grain, data):
    w = data.draw(st.lists(st.integers(min_value=0, max_value=50), min_size=n, max_size=n))
    out0 = data.draw(st.integers(min_value=1, max_value=9))
    src = _block(
        [f"out[i] = out[i // {c}] + w[i]"],
        "for i in range(1, n):",
        clauses=f"calibrate=false, grainsize={grain}",
    )
    env = {"n": n, "out": [out0] + [0] * (n - 1), "w": w}
    got = _run(src, env, "thread")
    assert got["out"] == _golden(src, env)["out"]


@given(n=st.integers(min_value=0, max_value=120), backend=BACKENDS, data=st.data())
def test_dict_insertion_order_matches_sequential(n, backend, data):
    keys = data.draw(
        st.lists(
            st.text(alphabet="abcdefghij", min_size=1, max_size=6),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    src = _block(["cache[key] = len(key) + offset"], "for key in keys:")
    env = {"keys": keys, "cache": {}, "offset": data.draw(st.integers(0, 5))}
    got = _run(src, env, backend)
    gold = _golden(src, env)
    assert bit_equal(got["cache"], gold["cache"])
    assert list(got["cache"].items()) == list(gold["cache"].items())


@given(
    expr=EXPR,
    n=st.integers(min_value=1, max_value=300),
    workers=st.integers(min_value=1, max_value=12),
    chunks=st.sampled_from([1, 2, 3, 8, 32]),
    data=st.data(),
)
def test_result_is_invariant_to_chunking(expr, n, workers, chunks, data):
    xs = data.draw(st.lists(st.integers(min_value=-500, max_value=500), min_size=n, max_size=n))
    src = _block(
        [f"ys[i] = {expr}"],
        "for i in range(len(xs)):",
        clauses=f"calibrate=false, backend=thread(chunks={chunks})",
    )
    env = {"xs": xs, "ys": [0] * n}
    got = _run(src, env, "thread", workers=workers)
    assert bit_equal(got["ys"], _golden(src, env)["ys"])
