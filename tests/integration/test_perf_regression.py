import os
import statistics
import time

import pytest

from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import select
from lucen.codegen import generate
from lucen.execution import dispatch
from lucen.execution.dispatch import execute, make_spec
from lucen.support import config

pytestmark = pytest.mark.perf


@pytest.fixture(autouse=True)
def _clean_state():
    config.set_active(config.Config())
    dispatch.reset_runtime_state()
    yield
    dispatch.reset_runtime_state()
    config.set_active(config.Config())


HEAVY = (
    "# LUCEN START\n"
    "for i in range(len(xs)):\n"
    "    acc = 0.0\n"
    "    for k in range(500):\n"
    "        acc = acc + xs[i] * 0.5 - k * 0.3\n"
    "    ys[i] = acc\n"
    "# LUCEN END\n"
)
LIGHT = "# LUCEN START\nfor i in range(len(xs)):\n    ys[i] = xs[i] * 2 + 1\n# LUCEN END\n"


def _spec(src):
    scan = scan_source(src, "t.py")
    analysis = analyze_source(src, scan, "t.py")[0]
    decision = select(analysis, workers=8)
    artifact = generate(analysis, decision, "t.py")
    return make_spec(analysis, decision, artifact)


def _median_ms(spec, n, backend, reps=3):
    for _ in range(1):
        env = {"xs": list(range(n)), "ys": [0.0] * n}
        execute(spec, range(n), env, force_backend=backend)
    samples = []
    for _ in range(reps):
        env = {"xs": list(range(n)), "ys": [0.0] * n}
        dispatch.reset_runtime_state()
        t0 = time.perf_counter()
        execute(spec, range(n), env, force_backend=backend)
        samples.append((time.perf_counter() - t0) * 1000)
    return statistics.median(samples)


SPEEDUP_FLOOR = 1.5


@pytest.mark.skipif((os.cpu_count() or 1) < 4, reason="speedup floor needs >= 4 cores")
def test_heavy_map_process_beats_sequential_by_the_floor():
    spec = _spec(HEAVY)
    n = 20_000
    seq = _median_ms(spec, n, "sequential")
    proc = _median_ms(spec, n, "process")
    ratio = seq / proc
    assert ratio >= SPEEDUP_FLOOR, (
        f"heavy map process speedup regressed: sequential {seq:.1f} ms, "
        f"process {proc:.1f} ms, ratio {ratio:.2f}x below floor {SPEEDUP_FLOOR}x"
    )


def test_heavy_map_routes_to_process_with_parallel_chunks():
    spec = _spec(HEAVY)
    n = 20_000
    env = {"xs": list(range(n)), "ys": [0.0] * n}
    execute(spec, range(n), env, force_backend=None)
    st = list(dispatch.get_block_stats().values())[-1]
    # Free-threaded builds reroute heavy PROCESS work to THREAD: with no GIL to
    # serialise the body there is no pickle/transfer cost to justify a process.
    assert st["backend"] == ("thread" if dispatch.free_threaded() else "process")
    assert st["parallel_runs"] == 1
    assert st["chunks"] > st["workers"]


def test_light_map_stays_sequential_no_dispatch_overhead():
    spec = _spec(LIGHT)
    n = 1_000_000
    env = {"xs": list(range(n)), "ys": [0] * n}
    execute(spec, range(n), env, force_backend=None)
    st = list(dispatch.get_block_stats().values())[-1]
    assert st["sequential_runs"] == 1
    assert st["parallel_runs"] == 0
