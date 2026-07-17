from __future__ import annotations

import pytest

from lucen.analysis.analyzer import DependencyShape, resolve_shapes
from lucen.analysis.rewriter import AuditTier, Classification, analyze_source
from lucen.analysis.scanner import scan_source
from lucen.support.errors import clear_fallback_report


@pytest.fixture(autouse=True)
def _clean_report():
    clear_fallback_report()
    yield
    clear_fallback_report()


def analyze(body, header="for i in range(1, n):"):
    body_src = "\n".join("    " + b for b in body)
    src = f"# LUCEN START\n{header}\n{body_src}\n# LUCEN END\n"
    scan = scan_source(src, "t.py")
    return analyze_source(src, scan, "t.py")[0]


def cls(analysis, name):
    return analysis.targets[name].classification


def test_stored_lambda_rejected():
    a = analyze(["fns[i] = lambda: xs[i]"], "for i in range(len(xs)):")
    assert not a.ok
    assert a.fallbacks[0].error == "IllegalSyntaxInBlockError"


def test_lambda_as_call_argument_allowed():
    a = analyze(["ys[i] = apply(lambda v: v + 1, xs[i])"], "for i in range(len(xs)):")
    assert a.ok
    assert cls(a, "ys") is Classification.SHARED_INDEXED_SAFE
    assert cls(a, "apply") is Classification.OUTER_READONLY


def test_stored_generator_rejected():
    a = analyze(["gens[i] = (v for v in xs[i])"], "for i in range(len(xs)):")
    assert not a.ok


def test_generator_as_call_argument_allowed():
    a = analyze(["ys[i] = sum(v * 2 for v in rows[i])"], "for i in range(len(xs)):")
    assert a.ok
    assert cls(a, "ys") is Classification.SHARED_INDEXED_SAFE


def test_listcomp_is_eager_and_allowed():
    a = analyze(["ys[i] = [v + 1 for v in rows[i]]"], "for i in range(len(xs)):")
    assert a.ok
    assert "v" not in a.targets


def test_walrus_local_does_not_leak():
    a = analyze(["ys[i] = (t := xs[i] * 2) + t"], "for i in range(len(xs)):")
    assert a.ok
    assert cls(a, "t") is Classification.LOOP_LOCAL


def test_tuple_unpack_locals():
    a = analyze(["a, b = xs[i], ys[i]", "out[i] = a + b"], "for i in range(len(xs)):")
    assert a.ok
    assert cls(a, "a") is Classification.LOOP_LOCAL
    assert cls(a, "b") is Classification.LOOP_LOCAL


def test_starred_unpack_local():
    a = analyze(["first, *rest = rows[i]", "out[i] = first"], "for i in range(len(xs)):")
    assert a.ok
    assert cls(a, "first") is Classification.LOOP_LOCAL
    assert cls(a, "rest") is Classification.LOOP_LOCAL


def test_nested_subscript_write_by_proof():
    a = analyze(["grid[i][0] = xs[i]"], "for i in range(len(xs)):")
    assert a.ok
    assert cls(a, "grid") is Classification.SHARED_INDEXED_SAFE
    assert a.targets["grid"].audit_tier is AuditTier.BY_PROOF


def test_attribute_then_subscript_chain():
    a = analyze(["obj.rows[i] = xs[i]"], "for i in range(len(xs)):")
    assert a.ok
    assert cls(a, "obj.rows") is Classification.SHARED_INDEXED_SAFE


def test_read_only_attribute_chain_is_outer():
    a = analyze(["out[i] = cfg.scale.factor * xs[i]"], "for i in range(len(xs)):")
    assert a.ok
    assert cls(a, "cfg") is Classification.OUTER_READONLY
    assert "cfg.scale" not in a.targets or cls(a, "cfg.scale") is Classification.OUTER_READONLY


def test_accumulator_read_into_local_blocks_reduction():
    a = analyze(
        ["snapshot = total", "total += xs[i]", "log[i] = snapshot"], "for i in range(len(xs)):"
    )
    assert a.ok
    assert (
        cls(a, "total") is not Classification.SHARED_SCALAR or a.targets["total"].reduce_op is None
    )


def test_count_pattern():
    a = analyze(["if xs[i] > 0:", "    count += 1"], "for i in range(len(xs)):")
    assert a.ok
    assert a.targets["count"].reduce_op == "+"


def test_two_independent_reductions():
    a = analyze(["total += xs[i]", "hi = max(hi, xs[i])"], "for i in range(len(xs)):")
    assert a.ok
    assert a.targets["total"].reduce_op == "+"
    assert a.targets["hi"].reduce_op == "max"


def test_mixed_ops_same_name_unresolved():
    a = analyze(["acc += xs[i]", "acc *= 2"], "for i in range(len(xs)):")
    assert a.ok
    assert a.targets["acc"].reduce_op is None


def test_self_write_then_self_read_is_self_contained():
    a = analyze(["out[i] = xs[i]", "out[i] = out[i] + 1"], "for i in range(len(xs)):")
    shapes = resolve_shapes(a)
    assert shapes["out"].shape is DependencyShape.SELF_CONTAINED


def test_negative_floordiv_not_recognized():
    a = analyze(["out[i] = out[i // -2]"])
    shapes = resolve_shapes(a)
    assert shapes["out"].shape is DependencyShape.UNRESOLVED


def test_shift_by_zero_not_recognized():
    a = analyze(["out[i] = out[i >> 0]"])
    shapes = resolve_shapes(a)
    assert shapes["out"].shape is DependencyShape.UNRESOLVED


def test_large_constant_fold():
    a = analyze(["out[i] = out[i // (2 ** 5)]"])
    shapes = resolve_shapes(a)
    assert shapes["out"].shape is DependencyShape.RECOGNIZED_DAG
    assert shapes["out"].divisor == 32


def test_mixed_self_and_monotonic_is_monotonic():
    a = analyze(["out[i] = out[i] + out[i - 1]"])
    shapes = resolve_shapes(a)
    assert shapes["out"].shape is DependencyShape.MONOTONIC_OFFSET


ILLEGAL = [
    (["with lock:", "    out[i] = xs[i]"], "with statement"),
    (["yield xs[i]"], "yield"),
    (["import os"], "import"),
    (["class C:", "    pass"], "class def"),
    (["async with x:", "    pass"], "async with"),
    (["global g", "g = 1"], "global"),
]


NESTED_NOW_SUPPORTED = [
    (["for j in range(3):", "    out[i] = xs[i] + j"], "nested for"),
    (
        ["if a[i]:", "    if b[i]:", "        out[i] = 1", "    else:", "        out[i] = 2"],
        "double nested if",
    ),
    (["c = 0", "while c < a[i]:", "    c = c + 1", "out[i] = c"], "while in for"),
]


@pytest.mark.parametrize("body,desc", NESTED_NOW_SUPPORTED)
def test_nested_control_flow_supported(body, desc):
    a = analyze(body, "for i in range(len(a)):")
    assert a.ok, desc
    assert cls(a, "out") is Classification.SHARED_INDEXED_SAFE


@pytest.mark.parametrize("body,desc", ILLEGAL)
def test_illegal_constructs(body, desc):
    a = analyze(body, "for i in range(len(a)):")
    assert not a.ok, desc


def test_del_on_shared_container_is_unresolved_not_crash():
    a = analyze(["del shared[i]"], "for i in range(len(shared)):")
    assert a.ok
    assert cls(a, "shared") is Classification.SHARED_INDEXED_UNRESOLVED


def test_condition_on_shared_scalar_illegal():
    a = analyze(
        ["total += xs[i]", "while total < 5:", "    total += 1"], "for i in range(len(xs)):"
    )
    assert not a.ok


def test_augmented_target_on_attribute():
    a = analyze(["acc.total += xs[i]"], "for i in range(len(xs)):")
    assert a.ok
    assert cls(a, "acc.total") is Classification.SHARED_SCALAR


def test_multiple_blocks_independent_classification():
    src = (
        "# LUCEN START\nfor i in range(len(a)):\n    a[i] = b[i]\n# LUCEN END\n"
        "# LUCEN START\nfor k in ks:\n    total += k\n# LUCEN END\n"
    )
    scan = scan_source(src, "t.py")
    analyses = analyze_source(src, scan, "t.py")
    assert len(analyses) == 2
    assert analyses[0].targets["a"].classification is Classification.SHARED_INDEXED_SAFE
    assert analyses[1].targets["total"].reduce_op == "+"


def test_try_finally_arms():
    a = analyze(
        ["try:", "    out[i] = risky(xs[i])", "finally:", "    seen[i] = 1"],
        "for i in range(len(xs)):",
    )
    assert a.ok
    assert cls(a, "out") is Classification.SHARED_INDEXED_SAFE
    assert cls(a, "seen") is Classification.SHARED_INDEXED_SAFE


def test_exception_name_is_scoped_local():
    a = analyze(
        ["try:", "    out[i] = xs[i]", "except ValueError as e:", "    out[i] = -1"],
        "for i in range(len(xs)):",
    )
    assert a.ok
    assert "e" not in a.targets
