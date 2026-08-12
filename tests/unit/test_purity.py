from __future__ import annotations

import builtins
import copy
import math
import os.path
import random
import sys

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
    assert reason == "writes shared state rooted at '_counter'"


def test_classifier_proves_mutating_method_on_global():
    verdict, reason = purity.classify(side_effect_note)
    assert verdict == purity.IMPURE
    assert reason == "mutates '_log' via .append()"


def test_classifier_propagates_through_call_chain():
    verdict, reason = purity.classify(calls_stateful)
    assert verdict == purity.IMPURE
    assert reason.startswith("calls 'stateful_tick' which ")


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


_chain_state: list = []


def _chain_leaf(x):
    _chain_state.append(x)
    return x


def _chain_3(x):
    return _chain_leaf(x)


def _chain_2(x):
    return _chain_3(x)


def _chain_1(x):
    return _chain_2(x)


def _local_accumulator(x):
    bucket = []
    bucket.append(x)
    return len(bucket)


def _calls_module_random(x):
    return random.randint(0, x)


def _calls_nested_attribute(x):
    return os.path.join("a", str(x))


_TABLE = [abs]


def _calls_through_a_table(x):
    return _TABLE[0](x)


_self_module = sys.modules[__name__]


def _calls_chain_via_module_attribute(x):
    return _self_module._chain_3(x)


def _calls_deep_chain_via_module_attribute(x):
    return _self_module._chain_1(x)


def _declares_globals(x):
    global _counter_a, _counter_b
    _counter_a = x
    _counter_b = x
    return x


def _outer_declares_nonlocal(x):
    seen = 0

    def inner():
        nonlocal seen
        seen += 1

    inner()
    return seen


def _function_without_source():
    ns: dict = {}
    exec("def hidden(v):\n    return v + 1\n", ns)
    return ns["hidden"]


@pytest.mark.parametrize("name", sorted(purity.IMPURE_BUILTIN_NAMES))
def test_every_named_impure_builtin_is_proved_impure(name):
    # Any name that stops matching is a hole in the guarantee: the block calling
    # it keeps its parallel routing. open is why the match is on the object the
    # name resolves to and not on __module__, which open reports as "io".
    verdict, reason = purity.classify(getattr(builtins, name))
    assert verdict == purity.IMPURE
    assert reason == f"'{name}' performs I/O or mutates state"


def test_stateful_modules_downgrade_their_callables():
    import logging

    verdict, reason = purity.classify(logging.getLogger)
    assert verdict == purity.IMPURE
    assert reason == "'getLogger' belongs to the stateful module 'logging'"
    assert purity.classify(random.randint)[0] == purity.IMPURE


def test_call_chain_is_followed_to_the_depth_limit_and_no_further():
    # Depth is the analyser's budget, and anything it cannot prove impure keeps
    # its routing, so this boundary decides which call chains stay parallel.
    # The memo is verdict-only, so it has to be cleared between the probes.
    for fn, expected in (
        (_chain_3, purity.IMPURE),
        (_chain_2, purity.IMPURE),
        (_chain_1, purity.PURE),
    ):
        purity.reset_memo()
        assert purity.classify(fn)[0] == expected, fn.__name__


def test_a_local_container_is_not_shared_state():
    # local_names is the union of locals and cellvars; narrowing it would report
    # every local mutation as a write to shared state and stop parallelising
    # blocks that are provably safe.
    assert purity.classify(_local_accumulator) == (purity.PURE, "")


def test_impurity_is_followed_through_a_module_attribute_call():
    verdict, reason = purity.classify(_calls_module_random)
    assert verdict == purity.IMPURE
    assert reason.startswith("calls 'random.randint' which ")


def test_a_module_attribute_call_spends_the_same_depth_budget():
    # The attribute branch keeps its own recursion into classify, so it can
    # drift from the plain-name branch and give dotted calls a shorter budget.
    purity.reset_memo()
    assert purity.classify(_calls_chain_via_module_attribute)[0] == purity.IMPURE
    purity.reset_memo()
    assert purity.classify(_calls_deep_chain_via_module_attribute)[0] == purity.PURE


def test_calls_that_are_not_plain_names_are_declined():
    # A dotted chain and a call through a subscript do not resolve to an object,
    # so the analyser has to decline rather than reach for .id.
    assert purity.classify(_calls_nested_attribute)[0] == purity.PURE
    assert purity.classify(_calls_through_a_table)[0] == purity.PURE


def test_declaring_global_or_nonlocal_is_proof_of_impurity():
    assert purity.classify(_declares_globals) == (
        purity.IMPURE,
        "declares global _counter_a, _counter_b",
    )
    assert purity.classify(_outer_declares_nonlocal) == (purity.IMPURE, "declares nonlocal seen")


def test_every_route_to_a_pure_verdict_carries_no_reason():
    purity.reset_memo()
    assert purity.classify(None) == (purity.PURE, "")
    assert purity.classify(math.sqrt) == (purity.PURE, "")
    assert purity.classify(42) == (purity.PURE, "")
    assert purity.classify(_chain_1) == (purity.PURE, "")
    assert purity.classify(_function_without_source()) == (purity.PURE, "")
    assert purity.classify(pure_math) == (purity.PURE, "")
    # the memo answers before the budget check, so the cut-off route needs a
    # cold cache to be reached at all
    purity.reset_memo()
    assert purity.classify(pure_math, 0) == (purity.PURE, "")


def test_verdicts_are_memoised_by_code_object():
    # The analyser walks a function once; re-walking it at every call site would
    # make classification quadratic in the call graph.
    purity.reset_memo()
    verdict = purity.classify(calls_stateful)
    assert purity._verdicts[id(calls_stateful.__code__)] == verdict
    purity._verdicts[id(calls_stateful.__code__)] = (purity.PURE, "from the memo")
    assert purity.classify(calls_stateful) == (purity.PURE, "from the memo")
    purity.reset_memo()


def test_random_in_body_runs_sequential_seeded_exact():
    src = block(["ys[i] = random.randint(0, 10 ** 9)"])
    _, spec = build(src)
    random.seed(999)
    env = {"xs": list(range(200)), "ys": [0] * 200, "random": random}
    execute(spec, range(200), env, force_backend="process")
    random.seed(999)
    expected = [random.randint(0, 10**9) for _ in range(200)]
    assert env["ys"] == expected
    assert dispatch.get_block_stats()[spec.key]["sequential_runs"] == 1
