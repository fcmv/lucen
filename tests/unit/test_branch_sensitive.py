from __future__ import annotations

import copy

import pytest

from lucen.analysis.rewriter import AuditTier, Classification, analyze_source
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

BS = frozenset({"branch_sensitive_deps"})


@pytest.fixture(autouse=True)
def _clean_state():
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config(experimental=BS))
    yield
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config())


CONFLICT_SRC = (
    "# LUCEN START calibrate=false\n"
    "for i in range(len(xs)):\n"
    "    if flags[i]:\n"
    "        out[i] = xs[i]\n"
    "    else:\n"
    "        out[dst[i]] = xs[i]\n"
    "# LUCEN END\n"
)


def analyze(src, experimental):
    scan = scan_source(src, "t.py")
    return analyze_source(src, scan, "t.py", experimental=experimental)[0]


def run(src, env, experimental=BS):
    a = analyze(src, experimental)
    d = select(a, workers=8, experimental=experimental)
    art = generate(a, d, "t.py")
    assert art is not None
    spec = make_spec(a, d, art)
    env = copy.deepcopy(env)
    execute(spec, range(len(env["xs"])), env, force_backend="thread")
    return env, spec


def golden(src, env):
    g = copy.deepcopy(env)
    exec(src, g)
    return g


def test_stable_rejects_conflict():
    a = analyze(CONFLICT_SRC, frozenset())
    assert not a.ok
    assert a.fallbacks[0].error == "BranchMergeConflictError"


def test_flag_relaxes_to_asserted_tier():
    a = analyze(CONFLICT_SRC, BS)
    assert a.ok
    info = a.targets["out"]
    assert info.classification is Classification.SHARED_INDEXED_SAFE
    assert info.audit_tier is AuditTier.ASSERTED
    d = select(a, workers=8, experimental=BS)
    assert d.eligibility is Eligibility.THREAD_CAPABLE
    assert d.audit_tier is AuditTier.ASSERTED


def test_disjoint_branches_run_parallel_and_correct():
    n = 200
    flags = [i < n // 2 for i in range(n)]
    dst = list(range(n))
    env = {"xs": list(range(n)), "out": [-1] * n, "flags": flags, "dst": dst}
    got, spec = run(CONFLICT_SRC, env)
    assert got["out"] == golden(CONFLICT_SRC, env)["out"]
    assert dispatch.get_block_stats()[spec.key]["parallel_runs"] == 1


def test_real_overlap_caught_and_rerun():
    n = 200
    flags = [i == 0 for i in range(n)]
    dst = [0] * n
    env = {"xs": list(range(1, n + 1)), "out": [-1] * n, "flags": flags, "dst": dst}
    got, spec = run(CONFLICT_SRC, env)
    assert got["out"] == golden(CONFLICT_SRC, env)["out"]
    assert dispatch.get_block_stats()[spec.key]["fallback_runs"] == 1
    assert any(r.error == "ParallelWriteConflictError" for r in get_fallback_report())


def test_same_index_read_in_one_branch_resolves_by_shape():
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(1, n):\n"
        "    if flags[i]:\n"
        "        out[i] = xs[i]\n"
        "    else:\n"
        "        out[i] = out[i - 1]\n"
        "# LUCEN END\n"
    )
    a = analyze(src, BS)
    assert a.ok
    assert a.targets["out"].classification is Classification.READ_AFTER_WRITE
    d = select(a, workers=8, experimental=BS)
    assert d.eligibility is Eligibility.SEQUENTIAL


def test_read_bearing_write_class_conflict_still_stands():
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(1, n):\n"
        "    if flags[i]:\n"
        "        out[i] = xs[i]\n"
        "    else:\n"
        "        out[dst[i]] = out[i - 1]\n"
        "# LUCEN END\n"
    )
    a = analyze(src, BS)
    assert not a.ok
    assert a.fallbacks[0].error == "BranchMergeConflictError"


def test_conditional_write_no_else_still_fine_without_flag():
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(len(xs)):\n"
        "    if xs[i] > 0:\n"
        "        out[i] = xs[i]\n"
        "# LUCEN END\n"
    )
    a = analyze(src, frozenset())
    assert a.ok
    assert a.targets["out"].audit_tier is AuditTier.BY_PROOF
