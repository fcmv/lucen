from __future__ import annotations

import ast
import copy
import random
import threading

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
    analysis = analyze_source(src, scan, "t.py")[0]
    decision = select(analysis, workers=8)
    artifact = generate(analysis, decision, "t.py")
    return analysis, make_spec(analysis, decision, artifact) if artifact else None


def run(src, env, backend="thread"):
    analysis, spec = build(src)
    assert spec is not None
    env = copy.deepcopy(env)
    it = analysis.for_node.iter
    if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "enumerate":
        iterable = eval(ast.unparse(it.args[0]), dict(env))
    else:
        iterable = eval(ast.unparse(it), dict(env))
    return execute(spec, iterable, env, force_backend=backend), env


def golden(src, env):
    g = copy.deepcopy(env)
    exec(src, g)
    return g


def test_pragma_free_source_never_scanned():
    src = "x = 1\nfor i in range(10):\n    x += i\ny = [j*2 for j in range(5)]\n"
    result = scan_source(src)
    assert result.pragmas == []
    assert result.blocks == []


def test_removing_pragmas_is_identical_execution():
    src = (
        "out = [0] * 100\n"
        "# LUCEN START calibrate=false\n"
        "for i in range(100):\n"
        "    out[i] = i * i\n"
        "# LUCEN END\n"
    )
    stripped = (
        "\n".join(line for line in src.splitlines() if not line.strip().startswith("# LUCEN"))
        + "\n"
    )
    g1: dict = {}
    exec(src, g1)
    g2: dict = {}
    exec(stripped, g2)
    assert g1["out"] == g2["out"]


def test_bytes_prefilter_matches_scanner():
    from lucen.analysis.scanner import PREFILTER_TOKEN

    samples = [
        "x = 1\n",
        "# LUCEN START\nfor i in r:\n    pass\n# LUCEN END\n",
        "s = 'LUCEN START'\n",
        "# just a comment\n",
    ]
    for src in samples:
        found = len(scan_source(src).pragmas) > 0
        prefilter = PREFILTER_TOKEN in src
        assert not found or prefilter


def test_concurrent_blocks_do_not_interfere():
    src_a = (
        "# LUCEN START calibrate=false\n"
        "for i in range(len(xs)):\n    ys[i] = xs[i] * 2\n# LUCEN END\n"
    )
    src_b = (
        "# LUCEN START calibrate=false\n"
        "for i in range(len(xs)):\n    ys[i] = xs[i] + 1000\n# LUCEN END\n"
    )
    _, spec_a = build(src_a)
    _, spec_b = build(src_b)
    results = {}
    errors = []

    def work(tag, spec, mult):
        try:
            for _ in range(20):
                n = 3000
                env = {"xs": list(range(n)), "ys": [0] * n}
                execute(spec, range(n), env, force_backend="thread")
                expected = [v * 2 for v in range(n)] if mult else [v + 1000 for v in range(n)]
                if env["ys"] != expected:
                    errors.append(tag)
                    return
            results[tag] = True
        except Exception as exc:  # noqa: BLE001
            errors.append((tag, exc))

    threads = [
        threading.Thread(target=work, args=("a", spec_a, True)),
        threading.Thread(target=work, args=("b", spec_b, False)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert results == {"a": True, "b": True}


@pytest.mark.parametrize("seed", range(8))
def test_random_disjoint_permutation_never_wrong(seed):
    rng = random.Random(seed + 900)
    n = rng.choice([16, 64, 200])
    perm = list(range(n))
    rng.shuffle(perm)
    src = (
        "# LUCEN START calibrate=false, depend=none\n"
        "for i in range(len(perm)):\n    out[perm[i]] = i * 3\n# LUCEN END\n"
    )
    env = {"perm": perm, "out": [-1] * n}
    for backend in ("thread", "process"):
        _, got = run(src, copy.deepcopy(env), backend)
        assert got["out"] == golden(src, env)["out"]


PARITY_CLAUSES = [
    "",
    "backend=thread",
    "backend=thread(pool_size=2)",
    "backend=thread(pool_size=16, chunks=8)",
    "reduction_order=sequential_equivalent",
    "progress=true",
    "nested=sequential",
]


@pytest.mark.parametrize("clauses", PARITY_CLAUSES)
def test_map_parity_across_clause_spellings(clauses):
    extra = f", {clauses}" if clauses else ""
    src = (
        f"# LUCEN START calibrate=false{extra}\n"
        "for i in range(len(xs)):\n    ys[i] = xs[i] * 3 + 1\n# LUCEN END\n"
    )
    env = {"xs": list(range(1500)), "ys": [0] * 1500}
    _, got = run(src, env)
    assert got["ys"] == [v * 3 + 1 for v in range(1500)]


@pytest.mark.parametrize(
    "clauses", ["", "reduction_order=sequential_equivalent", "backend=thread(pool_size=3)"]
)
def test_reduction_parity_across_spellings(clauses):
    extra = f", {clauses}" if clauses else ""
    src = (
        f"# LUCEN START calibrate=false{extra}\n"
        "for i in range(len(xs)):\n    total += xs[i] * 1.0001\n# LUCEN END\n"
    )
    env = {"xs": [0.1 * k + 0.03 for k in range(2500)], "total": 0.0}
    _, got = run(src, env)
    assert got["total"] == golden(src, env)["total"]


def test_naive_and_process_backend_agree():
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(len(xs)):\n    ys[i] = xs[i] * xs[i] - 7\n# LUCEN END\n"
    )
    env = {"xs": list(range(1000)), "ys": [0] * 1000}
    _, thread_env = run(src, copy.deepcopy(env), "thread")
    _, process_env = run(src, copy.deepcopy(env), "process")
    assert thread_env["ys"] == process_env["ys"] == golden(src, env)["ys"]
