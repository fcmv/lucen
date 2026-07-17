from __future__ import annotations

import copy
import math

import pytest

from lucen.analysis import purity
from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import select
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

_counter = [0]


def stateful_tick(x):
    _counter[0] += 1
    return _counter[0]


def pure_math(x):
    acc = 0.0
    for k in range(4):
        acc += math.sqrt(abs(x) + k)
    return acc


def calls_stateful(x):
    return stateful_tick(x) + 1


_log = []


def side_effect_note(x):
    _log.append(x)
    return x * 2


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
    analysis = analyze_source(src, scan, "t.py")[0]
    decision = select(analysis, workers=8)
    artifact = generate(analysis, decision, "t.py")
    assert artifact is not None
    return analysis, make_spec(analysis, decision, artifact)


def block(body, clauses="calibrate=false"):
    lines = "\n".join("    " + b for b in body)
    return f"# LUCEN START {clauses}\nfor i in range(len(xs)):\n{lines}\n# LUCEN END\n"


def test_classifier_proves_module_state_mutation():
    verdict, reason = purity.classify(stateful_tick)
    assert verdict == purity.IMPURE
    assert "_counter" in reason


def test_classifier_proves_mutating_method_on_global():
    verdict, reason = purity.classify(side_effect_note)
    assert verdict == purity.IMPURE
    assert "_log" in reason


def test_classifier_propagates_through_call_chain():
    verdict, reason = purity.classify(calls_stateful)
    assert verdict == purity.IMPURE
    assert "stateful_tick" in reason


def test_classifier_trusts_pure_and_c_level():
    import random

    assert purity.classify(pure_math)[0] == purity.PURE
    assert purity.classify(math.sqrt)[0] == purity.PURE
    assert purity.classify(print)[0] == purity.IMPURE
    assert purity.classify(random.randint)[0] == purity.IMPURE


def test_stateful_helper_runs_sequential_and_correct():
    src = block(["ys[i] = tick(xs[i])"])
    _, spec = build(src)
    _counter[0] = 0
    env = {"xs": list(range(600)), "ys": [0] * 600, "tick": stateful_tick}
    execute(spec, range(600), env, force_backend="process")
    assert env["ys"] == list(range(1, 601))
    assert dispatch.get_block_stats()[spec.key]["sequential_runs"] == 1
    assert any(
        r.error == "PreflightCheckError" and "tick" in r.message for r in get_fallback_report()
    )


def test_side_effects_preserved_and_ordered():
    src = block(["ys[i] = note(xs[i])"])
    _, spec = build(src)
    _log.clear()
    env = {"xs": list(range(300)), "ys": [0] * 300, "note": side_effect_note}
    execute(spec, range(300), env, force_backend="process")
    assert _log == list(range(300))
    assert dispatch.get_block_stats()[spec.key]["sequential_runs"] == 1


def test_pure_helper_keeps_parallel_routing():
    src = block(["ys[i] = f(xs[i])"])
    _, spec = build(src)
    env = {"xs": list(range(2000)), "ys": [0.0] * 2000, "f": pure_math}
    execute(spec, range(2000), env, force_backend="process")
    g = copy.deepcopy({"xs": env["xs"], "ys": [0.0] * 2000, "f": pure_math})
    exec(src, g)
    assert env["ys"] == g["ys"]
    assert dispatch.get_block_stats()[spec.key]["backend"] == "process"


def test_bench_helpers_classify_pure():
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "benchmarks"))
    try:
        import bench_helpers as bh

        for fn in (bh.light, bh.medium, bh.heavy, bh.combine):
            assert purity.classify(fn)[0] == purity.PURE, fn.__name__
    finally:
        sys.path.pop(0)


def test_trust_clause_restores_parallel():
    src = block(["ys[i] = tick(xs[i])"], clauses="calibrate=false, trust=callables")
    _, spec = build(src)
    _counter[0] = 0
    env = {"xs": list(range(600)), "ys": [0] * 600, "tick": stateful_tick}
    execute(spec, range(600), env, force_backend="process")
    assert dispatch.get_block_stats()[spec.key]["backend"] == "process"


def test_trust_pragma_on_def_restores_parallel():
    src = (
        "# LUCEN TRUST\n"
        "def local_tick(x):\n"
        "    _state.append(x)\n"
        "    return x + 1\n"
        "# LUCEN START calibrate=false\n"
        "for i in range(len(xs)):\n"
        "    ys[i] = local_tick(xs[i])\n"
        "# LUCEN END\n"
    )
    scan = scan_source(src, "t.py")
    assert "local_tick" in scan.trusted_names
    analysis = analyze_source(src, scan, "t.py")[0]
    assert "local_tick" in analysis.trusted_names
    decision = select(analysis, workers=8)
    artifact = generate(analysis, decision, "t.py")
    spec = make_spec(analysis, decision, artifact)
    assert "local_tick" in spec.trusted_names


def test_toml_trust_callables_restores_parallel(tmp_path):
    toml = tmp_path / "lucen.toml"
    toml.write_text('[trust]\ncallables = ["tick"]\n', encoding="utf-8")
    config.set_active(config.load(str(toml)))
    src = block(["ys[i] = tick(xs[i])"])
    _, spec = build(src)
    _counter[0] = 0
    env = {"xs": list(range(600)), "ys": [0] * 600, "tick": stateful_tick}
    execute(spec, range(600), env, force_backend="process")
    assert dispatch.get_block_stats()[spec.key]["backend"] == "process"


def test_random_in_body_runs_sequential_seeded_exact():
    import random

    src = block(["ys[i] = random.randint(0, 10 ** 9)"])
    _, spec = build(src)
    random.seed(999)
    env = {"xs": list(range(200)), "ys": [0] * 200, "random": random}
    execute(spec, range(200), env, force_backend="process")
    random.seed(999)
    expected = [random.randint(0, 10**9) for _ in range(200)]
    assert env["ys"] == expected
    assert dispatch.get_block_stats()[spec.key]["sequential_runs"] == 1
