from __future__ import annotations

import threading
import time

import pytest

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


@pytest.fixture(autouse=True)
def _clean_state():
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config())
    yield
    dispatch.shutdown()
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config())


def build(src):
    scan = scan_source(src, "t.py")
    a = analyze_source(src, scan, "t.py")[0]
    d = select(a, workers=8)
    art = generate(a, d, "t.py")
    assert art is not None
    return a, make_spec(a, d, art)


def block(body, header="for i in range(len(xs)):", clauses="calibrate=false"):
    body_src = "\n".join("    " + b for b in body)
    return f"# LUCEN START {clauses}\n{header}\n{body_src}\n# LUCEN END\n"


def run_concurrently(target, n_threads, repeats=1):
    barrier = threading.Barrier(n_threads)
    errors = []
    lock = threading.Lock()

    def worker(tid):
        try:
            barrier.wait()
            for r in range(repeats):
                target(tid, r)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append((tid, exc))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_same_map_block_from_many_threads():
    src = block(["ys[i] = xs[i] * 3 + 1"])
    _, spec = build(src)
    n = 2000
    expected = [v * 3 + 1 for v in range(n)]
    bad = []

    def target(tid, r):
        env = {"xs": list(range(n)), "ys": [0] * n}
        execute(spec, range(n), env, force_backend="thread")
        if env["ys"] != expected:
            bad.append((tid, r))

    errors = run_concurrently(target, n_threads=16, repeats=10)
    assert not errors, errors
    assert not bad, "a concurrent map produced a wrong result"


def test_different_blocks_concurrently():
    specs = []
    for mult in (2, 3, 5, 7):
        _, spec = build(block([f"ys[i] = xs[i] * {mult}"]))
        specs.append((mult, spec))
    n = 1500
    bad = []

    def target(tid, r):
        mult, spec = specs[tid % len(specs)]
        env = {"xs": list(range(n)), "ys": [0] * n}
        execute(spec, range(n), env, force_backend="thread")
        if env["ys"] != [v * mult for v in range(n)]:
            bad.append((tid, mult))

    errors = run_concurrently(target, n_threads=12, repeats=8)
    assert not errors and not bad


def test_reduction_concurrent_bit_identity():
    src = block(["total += xs[i] * 1.0001"])
    _, spec = build(src)
    vals = [0.1 * k + 0.03 for k in range(3000)]
    exp = 0.0
    for v in vals:
        exp += v * 1.0001
    bad = []

    def target(tid, r):
        env = {"xs": list(vals), "total": 0.0}
        execute(spec, range(len(vals)), env, force_backend="thread")
        if env["total"] != exp:
            bad.append(tid)

    errors = run_concurrently(target, n_threads=12, repeats=6)
    assert not errors and not bad


def test_chunk_interleaving_with_jitter():
    src = block(["ys[i] = slow(xs[i])"], header="for i in range(len(xs)):")
    _, spec = build(src)
    n = 200
    expected = [v + 1 for v in range(n)]
    bad = []

    def target(tid, r):
        env = {
            "xs": list(range(n)),
            "ys": [0] * n,
            "slow": lambda v: (time.sleep(0.0001), v + 1)[1],
        }
        execute(spec, range(n), env, force_backend="thread")
        if env["ys"] != expected:
            bad.append(tid)

    errors = run_concurrently(target, n_threads=8, repeats=5)
    assert not errors and not bad


def test_buffer_fast_path_concurrent():
    import array

    src = block(["ys[i] = xs[i] * 2.0 + 1.0"])
    _, spec = build(src)
    n = 3000
    expected = [v * 2.0 + 1.0 for v in range(n)]
    bad = []

    def target(tid, r):
        env = {
            "xs": array.array("d", [float(k) for k in range(n)]),
            "ys": array.array("d", [0.0] * n),
        }
        execute(spec, range(n), env, force_backend="thread")
        if list(env["ys"]) != expected:
            bad.append(tid)

    errors = run_concurrently(target, n_threads=10, repeats=6)
    assert not errors and not bad


def test_write_conflict_rerun_concurrent():
    src = "# LUCEN START calibrate=false\nfor key in keys:\n    seen[key] = key * 2\n# LUCEN END\n"
    _, spec = build(src)
    keys = ["a", "b", "c", "a", "d", "b", "e"]
    expected = {}
    for k in keys:
        expected[k] = k * 2
    bad = []

    def target(tid, r):
        env = {"keys": list(keys), "seen": {}}
        execute(spec, list(keys), env, force_backend="thread")
        if env["seen"] != expected:
            bad.append(tid)

    errors = run_concurrently(target, n_threads=10, repeats=6)
    assert not errors and not bad


def test_pool_saturation_no_deadlock():
    src = block(["ys[i] = xs[i] + tid_marker"], header="for i in range(len(xs)):")
    _, spec = build(src)
    n = 800
    bad = []

    def target(tid, r):
        env = {"xs": list(range(n)), "ys": [0] * n, "tid_marker": tid}
        execute(spec, range(n), env, force_backend="thread")
        if env["ys"] != [v + tid for v in range(n)]:
            bad.append(tid)

    errors = run_concurrently(target, n_threads=32, repeats=4)
    assert not errors and not bad


def test_nested_guard_concurrent():
    src = block(["ys[i] = xs[i] * 2"])
    _, spec = build(src)
    n = 1000
    expected = [v * 2 for v in range(n)]
    bad = []

    def target(tid, r):
        env = {"xs": list(range(n)), "ys": [0] * n}
        if tid % 2 == 0:
            with dispatch.nested_guard.dispatch_scope():
                execute(spec, range(n), env, force_backend="thread")
        else:
            execute(spec, range(n), env, force_backend="thread")
        if env["ys"] != expected:
            bad.append(tid)

    errors = run_concurrently(target, n_threads=12, repeats=6)
    assert not errors and not bad


def test_dag_wavefront_concurrent():
    src = block(
        ["out[i] = out[i // 2] + w[i]"],
        header="for i in range(1, n):",
        clauses="calibrate=false, grainsize=8",
    )
    _, spec = build(src)
    n = 1024

    def golden():
        g = {"n": n, "out": [1] + [0] * (n - 1), "w": list(range(n))}
        exec(src, g)
        return g["out"]

    expected = golden()
    bad = []

    def target(tid, r):
        env = {"n": n, "out": [1] + [0] * (n - 1), "w": list(range(n))}
        execute(spec, range(1, n), env, force_backend="thread")
        if env["out"] != expected:
            bad.append(tid)

    errors = run_concurrently(target, n_threads=8, repeats=5)
    assert not errors and not bad


def test_stats_run_count_not_lost_under_concurrency():
    src = block(["ys[i] = xs[i] + 1"], clauses="calibrate=false")
    _, spec = build(src)
    n = 500
    n_threads, repeats = 16, 40

    def target(tid, r):
        env = {"xs": list(range(n)), "ys": [0] * n}
        execute(spec, range(n), env, force_backend="thread")

    errors = run_concurrently(target, n_threads, repeats)
    assert not errors
    runs = dispatch.get_block_stats()[spec.key]["runs"]
    assert runs == n_threads * repeats, f"stats lost increments: {runs} != {n_threads * repeats}"


def test_fallback_report_intact_under_concurrency():
    src = "# LUCEN START calibrate=false\nfor key in keys:\n    seen[key] = key\n# LUCEN END\n"
    _, spec = build(src)
    keys = ["x", "y", "x", "z"]

    def target(tid, r):
        env = {"keys": list(keys), "seen": {}}
        execute(spec, list(keys), env, force_backend="thread")

    errors = run_concurrently(target, n_threads=12, repeats=10)
    assert not errors
    recs = get_fallback_report()
    assert all(hasattr(rec, "error") for rec in recs)


def test_memo_stable_under_concurrent_probes():
    src = block(["ys[i] = big(xs[i])"], header="for i in range(len(xs)):")
    _, spec = build(src)
    n = 4000
    expected = [v * 2 + 1 for v in range(n)]
    bad = []

    def target(tid, r):
        env = {"xs": list(range(n)), "ys": [0] * n, "big": lambda v: v * 2 + 1}
        execute(spec, range(n), env, force_backend="thread")
        if env["ys"] != expected:
            bad.append(tid)

    errors = run_concurrently(target, n_threads=16, repeats=5)
    assert not errors and not bad


def test_repeated_high_contention_determinism():
    map_src = block(["ys[i] = xs[i] * xs[i]"])
    red_src = block(["total += xs[i]"])
    _, map_spec = build(map_src)
    _, red_spec = build(red_src)
    n = 1200
    map_exp = [v * v for v in range(n)]
    red_exp = sum(range(n))
    bad = []

    def target(tid, r):
        if tid % 2 == 0:
            env = {"xs": list(range(n)), "ys": [0] * n}
            execute(map_spec, range(n), env, force_backend="thread")
            if env["ys"] != map_exp:
                bad.append(("map", tid))
        else:
            env = {"xs": list(range(n)), "total": 0}
            execute(red_spec, range(n), env, force_backend="thread")
            if env["total"] != red_exp:
                bad.append(("red", tid))

    errors = run_concurrently(target, n_threads=20, repeats=12)
    assert not errors and not bad
