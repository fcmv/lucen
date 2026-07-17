from __future__ import annotations

import array
import copy

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


def build(src):
    scan = scan_source(src, "t.py")
    a = analyze_source(src, scan, "t.py")[0]
    d = select(a, workers=8)
    art = generate(a, d, "t.py")
    return a, make_spec(a, d, art), art


MAP = (
    "# LUCEN START calibrate=false\n"
    "for i in range(len(xs)):\n"
    "    ys[i] = xs[i] * 2.0 + 1.0\n"
    "# LUCEN END\n"
)


def golden(src, env):
    g = copy.deepcopy(env)
    exec(src, g)
    return g


def test_pure_map_is_buffer_eligible():
    _, _, art = build(MAP)
    assert art.buffer_fast_path


def test_reduction_block_not_buffer_eligible():
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(len(xs)):\n    total += xs[i]\n# LUCEN END\n"
    )
    _, _, art = build(src)
    assert not art.buffer_fast_path


def test_dag_block_not_buffer_eligible():
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(1, n):\n    out[i] = out[i // 2] + w[i]\n# LUCEN END\n"
    )
    _, _, art = build(src)
    assert not art.buffer_fast_path


@pytest.mark.parametrize("typecode", ["d", "f"])
def test_array_output_direct_write_matches_sequential(typecode):
    _, spec, _ = build(MAP)
    n = 4000
    xs = array.array(typecode, [float(k) for k in range(n)])
    env = {"xs": xs, "ys": array.array(typecode, [0.0] * n)}
    execute(spec, range(n), env, force_backend="thread")
    g = golden(MAP, {"xs": xs, "ys": array.array(typecode, [0.0] * n)})
    assert list(env["ys"]) == list(g["ys"])
    assert dispatch.get_block_stats()[spec.key]["parallel_runs"] == 1


def test_int_array_output_with_int_body():
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(len(xs)):\n    ys[i] = xs[i] * 3 + 1\n"
        "# LUCEN END\n"
    )
    _, spec, art = build(src)
    assert art.buffer_fast_path
    n = 2000
    xs = array.array("i", list(range(n)))
    env = {"xs": xs, "ys": array.array("i", [0] * n)}
    execute(spec, range(n), env, force_backend="thread")
    assert list(env["ys"]) == [k * 3 + 1 for k in range(n)]


def test_bytearray_output():
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(len(xs)):\n    out[i] = (xs[i] * 3) % 256\n"
        "# LUCEN END\n"
    )
    _, spec, art = build(src)
    assert art.buffer_fast_path
    n = 1000
    env = {"xs": list(range(n)), "out": bytearray(n)}
    execute(spec, range(n), env, force_backend="thread")
    g = golden(src, {"xs": list(range(n)), "out": bytearray(n)})
    assert bytes(env["out"]) == bytes(g["out"])


def _spy_direct(monkeypatch):
    calls = []
    real = dispatch._run_buffer_direct

    def wrapper(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(dispatch, "_run_buffer_direct", wrapper)
    return calls


@pytest.mark.parametrize(
    "clause",
    [
        "on_error=collect",
        "timeout=5",
        "progress=true",
    ],
)
def test_instrumented_clauses_keep_slab_machinery(clause):
    src = (
        f"# LUCEN START calibrate=false, {clause}\n"
        "for i in range(len(xs)):\n"
        "    ys[i] = xs[i] * 2.0 + 1.0\n"
        "# LUCEN END\n"
    )
    _, _, art = build(src)
    assert art.buffer_fast_path is False


def test_list_output_takes_direct_path_and_is_correct(monkeypatch):
    calls = _spy_direct(monkeypatch)
    _, spec, _ = build(MAP)
    n = 4000
    env = {"xs": [float(i) for i in range(n)], "ys": [0.0] * n}
    execute(spec, range(n), env, force_backend="thread")
    assert env["ys"] == golden(MAP, {"xs": [float(i) for i in range(n)], "ys": [0.0] * n})["ys"]
    assert calls, "proven list map should use the direct-write fast path"


def test_list_subclass_keeps_slab_path(monkeypatch):
    calls = _spy_direct(monkeypatch)

    class Counting(list):
        writes = 0

        def __setitem__(self, i, v):
            type(self).writes += 1
            list.__setitem__(self, i, v)

    _, spec, _ = build(MAP)
    n = 2000
    Counting.writes = 0
    env = {"xs": [float(i) for i in range(n)], "ys": Counting([0.0] * n)}
    execute(spec, range(n), env, force_backend="thread")
    assert (
        list(env["ys"]) == golden(MAP, {"xs": [float(i) for i in range(n)], "ys": [0.0] * n})["ys"]
    )
    assert not calls, "list subclass must keep the transactional slab"
    assert Counting.writes == n


def test_conditional_write_list_direct_keeps_unwritten_slots(monkeypatch):
    calls = _spy_direct(monkeypatch)
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(len(xs)):\n"
        "    if xs[i] % 3 == 0:\n"
        "        ys[i] = xs[i] * 2.0\n"
        "# LUCEN END\n"
    )
    _, spec, _ = build(src)
    n = 3000
    env = {"xs": list(range(n)), "ys": [-7.0] * n}
    execute(spec, range(n), env, force_backend="thread")
    assert env["ys"] == golden(src, {"xs": list(range(n)), "ys": [-7.0] * n})["ys"]
    assert calls


@pytest.mark.parametrize("n", [1, 2, 7, 64, 999, 4096])
def test_buffer_direct_across_sizes(n):
    _, spec, _ = build(MAP)
    dispatch.reset_runtime_state()
    xs = array.array("d", [float(i) for i in range(n)])
    env = {"xs": xs, "ys": array.array("d", [0.0] * n)}
    result = execute(spec, range(n), env, force_backend="thread")
    g = golden(MAP, {"xs": xs, "ys": array.array("d", [0.0] * n)})
    assert list(env["ys"]) == list(g["ys"])
    assert result == (n - 1,)


def test_memoryview_output():
    _, spec, _ = build(MAP)
    n = 500
    backing = array.array("d", [0.0] * n)
    view = memoryview(backing)
    env = {"xs": array.array("d", [float(i) for i in range(n)]), "ys": view}
    execute(spec, range(n), env, force_backend="thread")
    assert list(backing) == [float(i) * 2.0 + 1.0 for i in range(n)]


def test_array_output_on_process_slab_path():
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(len(xs)):\n    ys[i] = xs[i] * 2 + 1\n# LUCEN END\n"
    )
    scan = scan_source(src, "t.py")
    a = analyze_source(src, scan, "t.py")[0]
    d = select(a, workers=8)
    spec = make_spec(a, d, generate(a, d, "t.py"))
    n = 1500
    env = {"xs": array.array("i", list(range(n))), "ys": array.array("i", [0] * n)}
    execute(spec, range(n), env, force_backend="process")
    assert list(env["ys"]) == [k * 2 + 1 for k in range(n)]


def test_enumerate_domain_buffer():
    src = (
        "# LUCEN START calibrate=false\n"
        "for idx, item in enumerate(items):\n    out[idx] = item * 2.0\n"
        "# LUCEN END\n"
    )
    _, spec, art = build(src)
    assert art.buffer_fast_path
    n = 300
    items = array.array("d", [float(i) for i in range(n)])
    env = {"items": items, "out": array.array("d", [0.0] * n)}
    execute(spec, items, env, force_backend="thread")
    assert list(env["out"]) == [float(i) * 2.0 for i in range(n)]
