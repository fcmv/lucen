from __future__ import annotations

import copy

import pytest

from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import select
from lucen.codegen import generate
from lucen.execution import dispatch, preflight
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
    return a, make_spec(a, d, art) if art else None, art


def test_resolves_dotted_module_function():
    fn = preflight._resolve("math.sqrt", {}, {})
    import math

    assert fn is math.sqrt


def test_resolves_deep_attribute_path():
    obj = preflight._resolve("collections.OrderedDict.fromkeys", {}, {})
    import collections

    assert obj == collections.OrderedDict.fromkeys


def test_unresolvable_returns_none():
    assert preflight._resolve("nonexistent_mod_xyz.foo", {}, {}) is None
    assert preflight._resolve("math.no_such_attr", {}, {}) is None


def test_env_and_globals_take_precedence_over_import():
    sentinel = object()
    assert preflight._resolve("math", {"math": sentinel}, {}) is sentinel
    assert preflight._resolve("x", {}, {"x": sentinel}) is sentinel


def test_custom_reduce_with_dotted_fn_engages():
    src = (
        "# LUCEN START calibrate=false, "
        "reduce=custom(fn=operator.add, identity=0)\n"
        "for i in range(len(xs)):\n    total += xs[i]\n# LUCEN END\n"
    )
    a, spec, _ = build(src)
    nums = list(range(1, 3001))
    env = {"xs": nums, "total": 0}
    execute(spec, range(len(nums)), env, force_backend="thread")
    assert env["total"] == sum(nums)


SIDE_EFFECT_SRC = (
    "# LUCEN START calibrate=false\nfor i in range(len(xs)):\n    sink.append(xs[i])\n# LUCEN END\n"
)


def test_side_effect_only_block_has_no_slabs():
    _, _, art = build(SIDE_EFFECT_SRC)
    assert art is None


def test_no_output_block_picks_thread_not_process(monkeypatch):
    class _Spec:
        clauses: dict = {}

        class artifact:
            slabs: list = []
            reductions: list = []
            inplace_mutation = False
            structured_payload = False

    monkeypatch.setattr(dispatch, "free_threaded", lambda: False)
    assert dispatch._pick_backend(_Spec()) == "thread"


def test_explicit_process_still_honored_for_no_output(monkeypatch):
    from lucen.analysis.scanner import parse_clause_text

    class _Spec:
        clauses = parse_clause_text("backend=process")

        class artifact:
            slabs: list = []
            reductions: list = []
            inplace_mutation = False
            structured_payload = False

    monkeypatch.setattr(dispatch, "free_threaded", lambda: False)
    assert dispatch._pick_backend(_Spec()) == "process"


def test_output_block_still_uses_process_on_gil(monkeypatch):
    class _Slab:
        pass

    class _Spec:
        clauses: dict = {}

        class artifact:
            slabs = [_Slab()]
            reductions: list = []
            inplace_mutation = False
            structured_payload = False

    monkeypatch.setattr(dispatch, "free_threaded", lambda: False)
    assert dispatch._pick_backend(_Spec()) == "process"


APPEND_SRC = (
    "# LUCEN START calibrate=false\n"
    "for i in range(len(xs)):\n"
    "    matrix[i].append(xs[i])\n"
    "# LUCEN END\n"
)


def test_inplace_mutation_flagged_and_thread_only():
    a, spec, art = build(APPEND_SRC)
    assert art.inplace_mutation
    monkeypatch_free = dispatch.free_threaded
    try:
        dispatch.free_threaded = lambda: False
        assert dispatch._pick_backend(spec) == "thread"
    finally:
        dispatch.free_threaded = monkeypatch_free


def test_inplace_mutation_correct_across_chunks():
    a, spec, _ = build(APPEND_SRC)
    n = 60
    matrix = [[k] for k in range(n)]
    env = {"xs": list(range(100, 100 + n)), "matrix": matrix}
    execute(spec, range(n), env, force_backend="thread")
    assert env["matrix"] == [[k, 100 + k] for k in range(n)]


NESTED_LOOP_SRC = (
    "# LUCEN START\n"
    "for i in range(len(rows)):\n"
    "    s = 0.0\n"
    "    for v in rows[i]:\n"
    "        s += v * 2.0\n"
    "    out[i] = s\n"
    "# LUCEN END\n"
)

DOUBLE_SUBSCRIPT_SRC = (
    "# LUCEN START\nfor i in range(len(rows)):\n    out[i] = rows[i][0] + rows[i][1]\n# LUCEN END\n"
)

UNSLICEABLE_STRUCTURED_SRC = (
    "# LUCEN START\n"
    "for i in range(len(out)):\n"
    "    s = 0.0\n"
    "    for v in rows[i % k]:\n"
    "        s += v * 2.0\n"
    "    out[i] = s\n"
    "# LUCEN END\n"
)

FLAT_NESTED_COMPUTE_SRC = (
    "# LUCEN START\n"
    "for i in range(len(xs)):\n"
    "    acc = 0.0\n"
    "    for k in range(50):\n"
    "        acc += xs[i] * k\n"
    "    ys[i] = acc\n"
    "# LUCEN END\n"
)


@pytest.mark.parametrize("src", [NESTED_LOOP_SRC, DOUBLE_SUBSCRIPT_SRC])
def test_sliceable_structured_uses_process_on_gil(monkeypatch, src):
    a, spec, art = build(src)
    assert art.structured_payload and art.sliceable == ("rows",)
    monkeypatch.setattr(dispatch, "free_threaded", lambda: False)
    assert dispatch._pick_backend(spec) == "process"


def test_sliceable_structured_uses_process_when_free_threaded(monkeypatch):
    a, spec, art = build(NESTED_LOOP_SRC)
    monkeypatch.setattr(dispatch, "free_threaded", lambda: True)
    assert dispatch._pick_backend(spec) == "process"


def test_unsliceable_structured_prefers_thread(monkeypatch):
    a, spec, art = build(UNSLICEABLE_STRUCTURED_SRC)
    assert art.structured_payload and art.sliceable == ()
    monkeypatch.setattr(dispatch, "free_threaded", lambda: False)
    assert dispatch._pick_backend(spec) == "thread"


def test_flat_nested_compute_still_uses_process_on_gil(monkeypatch):
    a, spec, art = build(FLAT_NESTED_COMPUTE_SRC)
    assert not art.structured_payload
    monkeypatch.setattr(dispatch, "free_threaded", lambda: False)
    assert dispatch._pick_backend(spec) == "process"


def test_sliceable_structured_result_correct():
    src = NESTED_LOOP_SRC.replace("# LUCEN START\n", "# LUCEN START calibrate=false\n")
    a, spec, _ = build(src)
    n = 500
    rows = [[float(j) for j in range(6)] for _ in range(n)]
    env = {"rows": copy.deepcopy(rows), "out": [0.0] * n}
    execute(spec, range(n), env)
    assert env["out"] == [sum(v * 2.0 for v in row) for row in rows]
    assert dispatch.get_block_stats()[spec.key]["backend"] == "process"


PLAIN_MAP_SRC = "# LUCEN START\nfor i in range(len(xs)):\n    ys[i] = xs[i] * 2 + 1\n# LUCEN END\n"


def test_map_routes_process_on_free_threaded(monkeypatch):
    a, spec, art = build(PLAIN_MAP_SRC)
    monkeypatch.setattr(dispatch, "free_threaded", lambda: True)
    assert dispatch._pick_backend(spec) == "process"


def test_map_routes_process_on_gil(monkeypatch):
    a, spec, art = build(PLAIN_MAP_SRC)
    monkeypatch.setattr(dispatch, "free_threaded", lambda: False)
    assert dispatch._pick_backend(spec) == "process"


@pytest.mark.parametrize("ft", [False, True])
def test_expert_backend_thread_override_honored(monkeypatch, ft):
    src = PLAIN_MAP_SRC.replace("# LUCEN START\n", "# LUCEN START backend=thread\n")
    a, spec, art = build(src)
    monkeypatch.setattr(dispatch, "free_threaded", lambda: ft)
    assert dispatch._pick_backend(spec) == "thread"


def test_inplace_mutation_stays_thread_on_free_threaded(monkeypatch):
    src = "# LUCEN START\nfor i in range(len(xs)):\n    matrix[i].append(xs[i])\n# LUCEN END\n"
    a, spec, art = build(src)
    assert art.inplace_mutation
    monkeypatch.setattr(dispatch, "free_threaded", lambda: True)
    assert dispatch._pick_backend(spec) == "thread"


def test_self_referential_assign_reduction_sequential_twin():
    src = (
        "# LUCEN START calibrate=false\nfor i in range(len(xs)):\n    s = s + xs[i]\n# LUCEN END\n"
    )
    a, spec, _ = build(src)
    _, seq_fn = spec.fns()
    assert "s" in spec.artifact.seq_params
    xs = [0.1 * k for k in range(500)]
    env = {"xs": xs, "s": 0.0}
    execute(spec, range(len(xs)), env, force_backend="thread")
    exp = 0.0
    for v in xs:
        exp = exp + v
    assert env["s"] == exp


def test_self_referential_assign_reduction_parallel():
    src = (
        "# LUCEN START calibrate=false\nfor i in range(len(xs)):\n    s = s + xs[i]\n# LUCEN END\n"
    )
    a, spec, _ = build(src)
    xs = [1.0] * 4000
    env = {"xs": xs, "s": 10.0}
    execute(spec, range(len(xs)), env, force_backend="process")
    assert env["s"] == 4010.0
