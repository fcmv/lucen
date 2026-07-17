from __future__ import annotations

import pytest

from lucen.analysis.rewriter import AuditTier, analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import Eligibility, select
from lucen.support.errors import (
    ErrorsMode,
    MonotonicDependencyError,
    UnprofitableParallelismError,
    UnresolvedDependencyShapeError,
    clear_fallback_report,
    set_errors_mode,
)


@pytest.fixture(autouse=True)
def _clean_state():
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    yield
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()


def decide(src: str, workers: int = 8):
    scan = scan_source(src, "t.py")
    analyses = analyze_source(src, scan, "t.py")
    assert len(analyses) == 1
    return select(analyses[0], workers=workers)


def block(body_lines, header="for i in range(1, n):", clauses=""):
    suffix = f" {clauses}" if clauses else ""
    body = "\n".join("    " + line for line in body_lines)
    return f"# LUCEN START{suffix}\n{header}\n{body}\n# LUCEN END\n"


def test_indexed_safe_is_thread_capable():
    d = decide(block(["out[i] = f(items[i]) * scale"]))
    assert d.eligibility is Eligibility.THREAD_CAPABLE
    assert d.audit_tier is AuditTier.BY_PROOF
    assert not d.unprofitable


def test_recognized_dag_is_wavefront():
    d = decide(block(["out[i] = combine(out[i // 2], w[i])"]))
    assert d.eligibility is Eligibility.WAVEFRONT
    assert d.dag_divisor == 2


def test_reduction_is_recognized():
    d = decide(block(["total += f(values[i])"]))
    assert d.eligibility is Eligibility.REDUCTION
    assert d.reduction_ops == {"total": "+"}


def test_monotonic_routes_sequential_informational():
    d = decide(block(["out[i] = out[i - 1] + values[i]"]))
    assert d.eligibility is Eligibility.SEQUENTIAL
    assert any(r.error == "MonotonicDependency" for r in d.fallbacks)


def test_modular_cycle_routes_sequential():
    d = decide(block(["out[i] = out[(i + 1) % n]"]))
    assert d.eligibility is Eligibility.SEQUENTIAL
    assert any(r.error == "DependencyCycleError" for r in d.fallbacks)


def test_unresolved_routes_sequential():
    d = decide(block(["out[perm[i]] = values[i]"]))
    assert d.eligibility is Eligibility.SEQUENTIAL
    assert any(r.error == "UnresolvedDependencyShapeError" for r in d.fallbacks)


def test_depend_none_asserts_thread_capable():
    d = decide(block(["out[perm[i]] = values[i]"], clauses="depend=none"))
    assert d.eligibility is Eligibility.THREAD_CAPABLE
    assert d.audit_tier is AuditTier.ASSERTED
    assert d.fallbacks == []


def test_depend_none_on_write_only_scatter_has_no_read_warning():
    d = decide(block(["out[perm[i]] = values[i]"], clauses="depend=none"))
    assert not d.asserted_read_residual


def test_depend_none_on_cross_read_flags_unverified_read():
    d = decide(
        block(
            ["out[i] = out[cross[i]] * 2 + xs[i]"],
            header="for i in range(len(xs)):",
            clauses="depend=none",
        )
    )
    assert d.eligibility is Eligibility.THREAD_CAPABLE
    assert d.asserted_read_residual
    assert any("NOT verified" in r for r in d.reasons)


def test_depend_acyclic_is_wavefront():
    d = decide(
        block(["out[order[i]] = f(out[parent[i]])"], clauses="depend=acyclic(order=my_mod.key)")
    )
    assert d.eligibility is Eligibility.WAVEFRONT


def test_break_routes_sequential():
    d = decide(block(["if values[i] < 0:", "    break", "out[i] = values[i]"]))
    assert d.eligibility is Eligibility.SEQUENTIAL
    assert any(r.error == "EarlyExitRouting" for r in d.fallbacks)


def test_unrecognized_scalar_merge_routes_sequential():
    d = decide(block(["if flags[i]:", "    winner = values[i]"]))
    assert d.eligibility is Eligibility.SEQUENTIAL
    assert any(r.error == "UnmergeableConflictError" for r in d.fallbacks)


def test_backend_sequential_is_honored():
    d = decide(block(["out[i] = values[i]"], clauses="backend=sequential"))
    assert d.eligibility is Eligibility.SEQUENTIAL
    assert d.fallbacks == []


def test_side_effect_only_block_is_thread_capable():
    d = decide(block(["process(items[i])"], header="for i in range(len(items)):"))
    assert d.eligibility is Eligibility.THREAD_CAPABLE


def test_failed_analysis_routes_sequential():
    d = decide(block(["with lock:", "    pass"]))
    assert d.eligibility is Eligibility.SEQUENTIAL


def test_strict_turns_downgrade_hard():
    src = block(["out[perm[i]] = values[i]"], clauses="strict=true")
    scan = scan_source(src, "t.py")
    analyses = analyze_source(src, scan, "t.py")
    with pytest.raises(UnresolvedDependencyShapeError):
        select(analyses[0])


def test_strict_turns_monotonic_hard():
    src = block(["out[i] = out[i - 1]"], clauses="strict=true")
    scan = scan_source(src, "t.py")
    analyses = analyze_source(src, scan, "t.py")
    with pytest.raises(MonotonicDependencyError):
        select(analyses[0])


def test_strict_allow_list_exempts_reason():
    d = decide(block(["out[i] = out[i - 1]"], clauses="strict=true(allow=[monotonic])"))
    assert d.eligibility is Eligibility.SEQUENTIAL


def test_tiny_known_block_is_statically_unprofitable():
    d = decide(block(["ys[i] = xs[i] * 2"], header="for i in range(200):"))
    assert d.eligibility is Eligibility.THREAD_CAPABLE
    assert d.unprofitable
    assert d.routed is Eligibility.SEQUENTIAL
    assert any(r.error == "PARALLEL_UNPROFITABLE" for r in d.fallbacks)


def test_large_known_block_is_not_condemned():
    d = decide(block(["ys[i] = xs[i] * 2"], header="for i in range(1000000):"))
    assert not d.unprofitable
    assert d.routed is Eligibility.THREAD_CAPABLE


def test_unknown_iteration_count_is_left_to_the_probe():
    d = decide(block(["ys[i] = xs[i] * 2"], header="for i in range(len(xs)):"))
    assert not d.unprofitable


def test_calibrate_false_skips_prescreen():
    d = decide(
        block(["ys[i] = xs[i] * 2"], header="for i in range(200):", clauses="calibrate=false")
    )
    assert not d.unprofitable
    assert d.routed is Eligibility.THREAD_CAPABLE


def test_process_overhead_far_exceeds_thread():
    from lucen.support import costmodel

    process = costmodel.overhead_ns(48, 1_000_000, thread=False)
    thread = costmodel.overhead_ns(48, 1_000_000, thread=True)
    assert process > thread * 10


def test_static_prescreen_condemns_hopeless_light_loop():
    from lucen.support import costmodel

    assert costmodel.statically_unprofitable(3, 2_000_000, workers=8)
    assert not costmodel.statically_unprofitable(50_000, 2_000_000, workers=8)


def test_strict_without_allowlist_makes_unprofitable_hard():
    src = block(["ys[i] = xs[i] * 2"], header="for i in range(200):", clauses="strict=true")
    scan = scan_source(src, "t.py")
    analyses = analyze_source(src, scan, "t.py")
    with pytest.raises(UnprofitableParallelismError):
        select(analyses[0], workers=8)


def test_strict_allow_unprofitable_keeps_quiet_routing():
    d = decide(
        block(
            ["ys[i] = xs[i] * 2"],
            header="for i in range(200):",
            clauses="strict=true(allow=[unprofitable])",
        )
    )
    assert d.unprofitable
    assert d.routed is Eligibility.SEQUENTIAL
