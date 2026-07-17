from __future__ import annotations

import array
import ast
import os
import statistics
import sys
import time

import bench_helpers as bh

from lucen.execution import _accel, dispatch
from lucen.support import config
from lucen.codegen import generate
from lucen.execution.dispatch import execute, make_spec
from lucen.analysis.rewriter import analyze_source
from lucen.execution.runtime import SKIP
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import select

REPS = 5


def timed(fn, reps=REPS):
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    return min(ts)


def build_spec(src):
    scan = scan_source(src, "bench.py")
    a = analyze_source(src, scan, "bench.py")[0]
    d = select(a, workers=os.cpu_count() or 8)
    art = generate(a, d, "bench.py")
    return a, d, (make_spec(a, d, art) if art else None)


def iterable_for(analysis, env):
    it = analysis.for_node.iter
    if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "enumerate":
        return eval(ast.unparse(it.args[0]), dict(env))
    return eval(ast.unparse(it), dict(env))


def _wl(header, body, clauses=""):
    head = f"# LUCEN START {clauses}".rstrip()
    return head + f"\n{header}\n" + "\n".join("    " + b for b in body) + "\n# LUCEN END\n"


WORKLOADS = [
    (
        "light map",
        1_000_000,
        _wl("for i in range(len(xs)):", ["ys[i] = xs[i] * 2 + 1"]),
        lambda n: {"xs": list(range(n)), "ys": [0] * n},
    ),
    (
        "medium map",
        40_000,
        _wl("for i in range(len(xs)):", ["ys[i] = medium(xs[i])"]),
        lambda n: {"xs": list(range(n)), "ys": [0.0] * n, "medium": bh.medium},
    ),
    (
        "heavy map",
        20_000,
        _wl("for i in range(len(xs)):", ["ys[i] = heavy(xs[i])"]),
        lambda n: {"xs": list(range(n)), "ys": [0.0] * n, "heavy": bh.heavy},
    ),
    (
        "light reduction",
        1_000_000,
        _wl("for i in range(len(xs)):", ["total += xs[i]"]),
        lambda n: {"xs": list(range(n)), "total": 0},
    ),
    (
        "heavy reduction",
        20_000,
        _wl("for i in range(len(xs)):", ["total += heavy(xs[i])"]),
        lambda n: {"xs": list(range(n)), "total": 0.0, "heavy": bh.heavy},
    ),
    (
        "recognized DAG",
        100_000,
        _wl(
            "for i in range(1, n):",
            ["results[i] = combine(results[i // 2], w[i])"],
            clauses="grainsize=64",
        ),
        lambda n: {
            "n": n,
            "results": [1.0] + [0.0] * (n - 1),
            "w": [float(k) for k in range(n)],
            "combine": bh.combine,
        },
    ),
    (
        "buffer map (array)",
        1_000_000,
        _wl("for i in range(len(xs)):", ["ys[i] = xs[i] * 2.0 + 1.0"]),
        lambda n: {
            "xs": array.array("d", [float(k) for k in range(n)]),
            "ys": array.array("d", bytes(8 * n)),
        },
    ),
    (
        "nested heavy",
        4_000,
        _wl(
            "for i in range(len(rows)):",
            ["s = 0.0", "for v in rows[i]:", "    s += medium(v)", "out[i] = s"],
        ),
        lambda n: {
            "rows": [[float(j) for j in range(6)] for _ in range(n)],
            "out": [0.0] * n,
            "medium": bh.medium,
        },
    ),
]


def fmt(t):
    return f"{t * 1000:.2f}"


def row(cols, widths):
    return "| " + " | ".join(str(c).ljust(w) for c, w in zip(cols, widths)) + " |"


def sep(widths):
    return "|" + "|".join("-" * (w + 2) for w in widths) + "|"


def main():
    config.set_active(config.Config())
    print(
        f"Lucen benchmark  |  cores={os.cpu_count()}  "
        f"python={sys.version.split()[0]}  native={_accel.ACCELERATED}  "
        f"best-of-{REPS}\n"
    )

    print("## 1. Native primitives (native vs pure-Python)\n")
    if not _accel.ACCELERATED:
        print(
            "Native core not loaded on this interpreter (expected on free-threaded "
            "and pure-wheel installs); every primitive runs the pure-Python "
            "fallback, so there is nothing to compare here.\n"
        )
    else:
        w = [36, 12, 12, 9]
        print(row(["primitive", "python(ms)", "native(ms)", "ratio"], w))
        print(sep(w))
        for name, py_t, nat_t in _native_primitives():
            print(row([name, fmt(py_t), fmt(nat_t), f"{py_t / nat_t:.2f}x"], w))

    print("\n## 2. End-to-end: Lucen (gate on) vs plain sequential\n")
    w2 = [20, 10, 11, 12, 8, 11]
    print(row(["workload", "n", "plain(ms)", "lucen(ms)", "speedup", "backend"], w2))
    print(sep(w2))
    forced = []
    for name, n, src, factory in WORKLOADS:
        analysis, decision, spec = build_spec(src)
        if spec is None:
            base = timed(lambda: exec(src, factory(n)))
            print(row([name, f"{n:,}", fmt(base), "-", "1.00x", "seq(fallback)"], w2))
            continue
        dispatch.reset_runtime_state()
        base = timed(
            lambda: execute(
                spec, iterable_for(analysis, (e := factory(n))), e, force_backend="sequential"
            )
        )
        dispatch.reset_runtime_state()
        got = timed(lambda: execute(spec, iterable_for(analysis, (e := factory(n))), e))
        st = list(dispatch.get_block_stats().values())
        backend = st[-1]["backend"] if st else "?"
        print(row([name, f"{n:,}", fmt(base), fmt(got), f"{base / got:.2f}x", backend], w2))
        forced.append((name, n, spec, analysis, factory, base))

    print("\n## 3. End-to-end forced parallel (gate bypassed) -- raw machinery\n")
    w3 = [20, 11, 11, 12, 8, 8]
    print(row(["workload", "plain(ms)", "thread(ms)", "process(ms)", "thr", "proc"], w3))
    print(sep(w3))
    import lucen.support.costmodel as cm

    cm.statically_unprofitable = lambda *a, **k: False
    dispatch._profitable = lambda *a, **k: True
    for name, n, spec, analysis, factory, base in forced:
        dispatch.reset_runtime_state()
        th = timed(
            lambda: execute(
                spec, iterable_for(analysis, (e := factory(n))), e, force_backend="thread"
            )
        )
        try:
            pr = timed(
                lambda: execute(
                    spec, iterable_for(analysis, (e := factory(n))), e, force_backend="process"
                ),
                reps=3,
            )
        except Exception:  # noqa: BLE001
            pr = None
        prcell = fmt(pr) if pr else "n/a"
        prx = f"{base / pr:.2f}x" if pr else "n/a"
        print(row([name, fmt(base), fmt(th), prcell, f"{base / th:.2f}x", prx], w3))

    dispatch.shutdown()
    from lucen.execution import process_backend

    process_backend.shutdown()
    routed = "THREAD" if dispatch.free_threaded() else "PROCESS"
    engine_note = (
        "With the GIL disabled, THREAD parallelises pure-Python bodies directly, "
        "so CPU-bound work is routed to THREAD with no pickling or subprocess; "
        "PROCESS is kept only for the rare shape where it still wins."
        if dispatch.free_threaded()
        else "On a GIL build, THREAD cannot speed up pure-Python bodies (the GIL "
        "serialises them), so the real gains come from PROCESS on CPU-bound work."
    )
    print(
        "\nRead: baselines run the SAME generated loop sequentially (a function "
        "with fast LOAD_FAST locals), so the numbers isolate what parallelism "
        "adds, not the ~1.5x that comes for free from running a loop in a "
        "function versus exec() in a globals dict. " + engine_note + " Section 2 "
        "is what a user gets: the gate keeps light work and the recognized-DAG "
        "shape SEQUENTIAL (~1.0x, no help and no harm) and sends CPU-bound work "
        f"(medium and heavy maps, heavy reduction, nested heavy) to {routed}. "
        "Range inputs read as xs[i] ship as per-chunk slices (spec 5.10), not the "
        "whole array per chunk, so mid-weight work wins too. Section 3 bypasses "
        "the gate to show the raw per-backend cost it is steering around."
    )


def _native_primitives():
    import random

    rng = random.Random(0)
    n = 2_000_000
    step = n // 48
    chunks = [list(range(a, min(a + step, n))) for a in range(0, n, step)]
    sites = [[rng.uniform(-1e3, 1e3) for _ in range(n)]]

    def audit_native():
        return _accel.audit_index_bitmap(chunks, n)

    def audit_python():
        seen = set()
        for idx in chunks:
            local = set(idx)
            if not seen.isdisjoint(local):
                return -1
            seen.update(local)
        return None

    def fold_native():
        return _accel.fold_ordered(0.0, sites, "+", SKIP)

    def fold_python():
        acc = 0.0
        for slab in sites:
            for v in slab:
                if v is not SKIP:
                    acc += v
        return acc

    return [
        ("write-set audit (2M idx, 48 chunks)", timed(audit_python, 3), timed(audit_native, 3)),
        ("reduction fold (2M floats)", timed(fold_python, 3), timed(fold_native, 3)),
    ]


if __name__ == "__main__":
    main()
