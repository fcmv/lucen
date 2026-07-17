from __future__ import annotations

import array
import statistics
import sys
import time
import types

from lucen.support import config
from lucen.execution import dispatch
from lucen.execution.dispatch import execute

from bench import WORKLOADS, build_spec, iterable_for

WARMUP = 1
REPS = 11
COOLDOWN_S = 2.0


def _data_keys(factory, n: int) -> set:
    return {
        k for k, v in factory(n).items() if not callable(v) and not isinstance(v, types.ModuleType)
    }


def _bit_identical(a: dict, b: dict, keys: set) -> bool:
    for k in keys:
        x, y = a[k], b[k]
        if isinstance(x, array.array):
            x = list(x)
        if isinstance(y, array.array):
            y = list(y)
        if x != y:
            return False
    return True


def _golden(src: str, factory, n: int) -> dict:
    g = factory(n)
    exec(src, g)
    return g


def _actual_backend() -> str:
    st = list(dispatch.get_block_stats().values())
    return st[-1]["backend"] if st else "?"


def _run(spec, analysis, env, backend=None):
    dispatch.reset_runtime_state()
    execute(spec, iterable_for(analysis, env), env, force_backend=backend)
    return _actual_backend()


def _timed_median(spec, analysis, factory, n, backend):
    time.sleep(COOLDOWN_S)
    for _ in range(WARMUP):
        _run(spec, analysis, factory(n), backend)
    times, actual = [], None
    for _ in range(REPS):
        env = factory(n)
        t0 = time.perf_counter()
        actual = _run(spec, analysis, env, backend)
        times.append(time.perf_counter() - t0)
    return statistics.median(times), actual


def main():
    config.set_active(config.Config())
    ft = not (getattr(sys, "_is_gil_enabled", lambda: True)())
    print(
        f"Backend-selection validation on {sys.version.split()[0]} "
        f"({'free-threaded' if ft else 'GIL'});  timing = median of {REPS} "
        f"reps after warm-up\n"
    )

    correctness_rows, routing_rows, mismatches = [], [], []

    for name, n, src, factory in WORKLOADS:
        analysis, decision, spec = build_spec(src)
        if spec is None:
            continue

        keys = _data_keys(factory, n)
        gold = _golden(src, factory, n)
        verdict = {}
        for label, bk in (
            ("seq", "sequential"),
            ("thr", "thread"),
            ("proc", "process"),
            ("gate", None),
        ):
            env = factory(n)
            _run(spec, analysis, env, bk)
            verdict[label] = _bit_identical(env, gold, keys)
        correctness_rows.append((name, verdict))

        by_actual = {}
        for bk in ("sequential", "thread", "process"):
            med, actual = _timed_median(spec, analysis, factory, n, bk)
            by_actual[actual] = min(by_actual.get(actual, med), med)
        gate_med, gate_pick = _timed_median(spec, analysis, factory, n, None)
        fastest = min(by_actual, key=by_actual.get)
        match = gate_pick == fastest
        routing_rows.append((name, by_actual, fastest, gate_pick, match))
        if not match:
            mismatches.append((name, by_actual, fastest, gate_pick, gate_med))

    _print_correctness(correctness_rows)
    _print_routing(routing_rows)
    _print_mismatches(mismatches)

    dispatch.shutdown()
    from lucen.execution import process_backend

    process_backend.shutdown()


def _print_correctness(rows):
    w = [20, 6, 6, 6, 6, 10]
    print("## 1. Correctness (bit-identical to plain Python)\n")
    print(_row(["workload", "seq", "thread", "process", "gate", "all match?"], w))
    print(_sep(w))
    for name, v in rows:
        mark = {True: "ok", False: "DIFF"}
        allok = all(v.values())
        print(
            _row(
                [
                    name,
                    mark[v["seq"]],
                    mark[v["thr"]],
                    mark[v["proc"]],
                    mark[v["gate"]],
                    "yes" if allok else "NO",
                ],
                w,
            )
        )
    print()


def _print_routing(rows):
    w = [20, 11, 11, 11, 11, 11, 8]
    print("## 2. Routing (median ms per actual backend; gate must match fastest)\n")
    print(_row(["workload", "seq", "thread", "process", "fastest", "gate pick", "match?"], w))
    print(_sep(w))
    for name, by_actual, fastest, gate_pick, match in rows:

        def cell(bk):
            return f"{by_actual[bk] * 1000:.1f}" if bk in by_actual else "  -"

        print(
            _row(
                [
                    name,
                    cell("sequential"),
                    cell("thread"),
                    cell("process"),
                    fastest,
                    gate_pick,
                    "yes" if match else "NO",
                ],
                w,
            )
        )
    print()


def _print_mismatches(mismatches):
    if not mismatches:
        print(
            "All workloads: the gate picked the fastest backend, and every "
            "backend is bit-identical to plain Python."
        )
        return
    print("## Mismatches (gate did not pick the fastest -- reproducible)\n")
    for name, by_actual, fastest, gate_pick, gate_med in mismatches:
        picked_ms = by_actual.get(gate_pick, gate_med) * 1000
        fastest_ms = by_actual[fastest] * 1000
        print(
            f"  {name}: gate picked {gate_pick} ({picked_ms:.1f} ms), "
            f"fastest is {fastest} ({fastest_ms:.1f} ms), "
            f"gap {picked_ms / fastest_ms:.2f}x"
        )


def _row(cols, widths):
    return "| " + " | ".join(str(c).ljust(w) for c, w in zip(cols, widths)) + " |"


def _sep(widths):
    return "|" + "|".join("-" * (w + 2) for w in widths) + "|"


if __name__ == "__main__":
    main()
