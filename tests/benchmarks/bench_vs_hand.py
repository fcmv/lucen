import array
import os
import statistics
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, wait

import bench_helpers as bh
import hand_workers as hw

import lucen.execution.dispatch as D
from lucen.support import config
from lucen.execution import dispatch
from bench import WORKLOADS, build_spec, iterable_for

REPS = 5
NCORE = os.cpu_count() or 8
_tpool = ThreadPoolExecutor(max_workers=NCORE)
_ppool = ProcessPoolExecutor(max_workers=NCORE)


def med(fn):
    time.sleep(1.0)
    fn()
    ts = []
    for _ in range(REPS):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    return statistics.median(ts) * 1000


def bounds(n, parts=NCORE):
    step = -(-n // parts) if n else 1
    return [(a, min(a + step, n)) for a in range(0, n, step)]


def hand_impls(name, n, factory):
    e = factory(n)

    if name == "light map":
        xs = e["xs"]

        def seq():
            ys = [0] * n
            for i in range(n):
                ys[i] = xs[i] * 2 + 1
            return ys

        def thr():
            ys = [0] * n

            def part(a, b):
                for i in range(a, b):
                    ys[i] = xs[i] * 2 + 1

            wait([_tpool.submit(part, a, b) for a, b in bounds(n)])
            return ys

        def proc():
            futs = [_ppool.submit(hw.w_light, xs[a:b]) for a, b in bounds(n)]
            ys = []
            for f in futs:
                ys.extend(f.result())
            return ys

        return seq, thr, proc

    if name in ("medium map", "heavy map"):
        xs = e["xs"]
        kernel = bh.medium if name == "medium map" else bh.heavy
        worker = hw.w_medium if name == "medium map" else hw.w_heavy

        def seq():
            ys = [0.0] * n
            for i in range(n):
                ys[i] = kernel(xs[i])
            return ys

        def thr():
            ys = [0.0] * n

            def part(a, b):
                for i in range(a, b):
                    ys[i] = kernel(xs[i])

            wait([_tpool.submit(part, a, b) for a, b in bounds(n)])
            return ys

        def proc():
            futs = [_ppool.submit(worker, xs[a:b]) for a, b in bounds(n)]
            ys = []
            for f in futs:
                ys.extend(f.result())
            return ys

        return seq, thr, proc

    if name == "light reduction":
        xs = e["xs"]

        def seq():
            total = 0
            for i in range(n):
                total += xs[i]
            return total

        def thr():
            parts = [0] * NCORE

            def part(k, a, b):
                s = 0
                for i in range(a, b):
                    s += xs[i]
                parts[k] = s

            wait([_tpool.submit(part, k, a, b) for k, (a, b) in enumerate(bounds(n))])
            return sum(parts)

        def proc():
            futs = [_ppool.submit(hw.w_light_sum, xs[a:b]) for a, b in bounds(n)]
            return sum(f.result() for f in futs)

        return seq, thr, proc

    if name == "heavy reduction":
        xs = e["xs"]

        def seq():
            total = 0.0
            for i in range(n):
                total += bh.heavy(xs[i])
            return total

        def thr():
            parts = [0.0] * NCORE

            def part(k, a, b):
                s = 0.0
                for i in range(a, b):
                    s += bh.heavy(xs[i])
                parts[k] = s

            wait([_tpool.submit(part, k, a, b) for k, (a, b) in enumerate(bounds(n))])
            return sum(parts)

        def proc():
            futs = [_ppool.submit(hw.w_heavy_sum, xs[a:b]) for a, b in bounds(n)]
            return sum(f.result() for f in futs)

        return seq, thr, proc

    if name == "recognized DAG":
        w = e["w"]

        def seq():
            results = [1.0] + [0.0] * (n - 1)
            for i in range(1, n):
                results[i] = bh.combine(results[i // 2], w[i])
            return results

        def thr():
            results = [1.0] + [0.0] * (n - 1)

            def part(a, b):
                for i in range(a, b):
                    results[i] = bh.combine(results[i // 2], w[i])

            lo = 1
            while lo < n:
                hi = min(lo * 2, n)
                wait([_tpool.submit(part, a, b) for a, b in bounds_range(lo, hi)])
                lo = hi
            return results

        def proc():
            results = [1.0] + [0.0] * (n - 1)
            lo = 1
            while lo < n:
                hi = min(lo * 2, n)
                futs = []
                spans = bounds_range(lo, hi)
                for a, b in spans:
                    parents = [results[i // 2] for i in range(a, b)]
                    futs.append(_ppool.submit(hw.w_dag_level, parents, w[a:b]))
                for (a, b), f in zip(spans, futs):
                    results[a:b] = f.result()
                lo = hi
            return results

        return seq, thr, proc

    if name == "buffer map (array)":
        xs = e["xs"]

        def seq():
            ys = array.array("d", bytes(8 * n))
            for i in range(n):
                ys[i] = xs[i] * 2.0 + 1.0
            return ys

        def thr():
            ys = array.array("d", bytes(8 * n))

            def part(a, b):
                for i in range(a, b):
                    ys[i] = xs[i] * 2.0 + 1.0

            wait([_tpool.submit(part, a, b) for a, b in bounds(n)])
            return ys

        def proc():
            futs = [_ppool.submit(hw.w_buffer, xs[a:b]) for a, b in bounds(n)]
            ys = array.array("d")
            for f in futs:
                ys.extend(f.result())
            return ys

        return seq, thr, proc

    if name == "nested heavy":
        rows = e["rows"]

        def seq():
            out = [0.0] * n
            for i in range(n):
                s = 0.0
                for v in rows[i]:
                    s += bh.medium(v)
                out[i] = s
            return out

        def thr():
            out = [0.0] * n

            def part(a, b):
                for i in range(a, b):
                    s = 0.0
                    for v in rows[i]:
                        s += bh.medium(v)
                    out[i] = s

            wait([_tpool.submit(part, a, b) for a, b in bounds(n)])
            return out

        def proc():
            futs = [_ppool.submit(hw.w_nested, rows[a:b]) for a, b in bounds(n)]
            out = []
            for f in futs:
                out.extend(f.result())
            return out

        return seq, thr, proc

    raise AssertionError(name)


def bounds_range(lo, hi, parts=NCORE):
    n = hi - lo
    if n <= 0:
        return []
    step = max(1, -(-n // parts))
    return [(a, min(a + step, hi)) for a in range(lo, hi, step)]


def plx_runner(spec, analysis, env, n, backend):
    it = iterable_for(analysis, env)
    scalars = [k for k, v in env.items() if isinstance(v, (int, float)) and k not in ("n",)]
    zero = {k: type(env[k])() for k in scalars}

    def run():
        for k, z in zero.items():
            env[k] = z
        dispatch.reset_runtime_state()
        D.execute(spec, it, env, force_backend=backend)

    return run


def main():
    config.set_active(config.Config())
    D._profitable = lambda *a, **k: True
    import lucen.support.costmodel as cm

    cm.statically_unprofitable = lambda *a, **k: False

    _ppool.submit(hw.w_light, [1]).result()

    rows = []
    for name, n, src, factory in WORKLOADS:
        analysis, _, spec = build_spec(src)
        env = factory(n)
        h_seq, h_thr, h_proc = hand_impls(name, n, factory)
        t = {}
        for way, backend, hand_fn in (
            ("seq", "sequential", h_seq),
            ("thr", "thread", h_thr),
            ("proc", "process", h_proc),
        ):
            t[f"plx_{way}"] = med(plx_runner(spec, analysis, env, n, backend))
            t[f"hand_{way}"] = med(hand_fn)
        rows.append((name, t))
        print(f"[done] {name}")

    print(f"\nLucen vs hand-written (median of {REPS}, ms; cores={NCORE}, GIL 3.11)\n")
    hdr = ["workload", "way", "lucen", "hand", "delta", "ratio"]
    w = [20, 6, 10, 10, 9, 7]
    print("| " + " | ".join(h.ljust(x) for h, x in zip(hdr, w)) + " |")
    print("|" + "|".join("-" * (x + 2) for x in w) + "|")
    for name, t in rows:
        for way, pk, hk in (
            ("seq", "plx_seq", "hand_seq"),
            ("thr", "plx_thr", "hand_thr"),
            ("proc", "plx_proc", "hand_proc"),
        ):
            d = t[pk] - t[hk]
            r = t[pk] / t[hk] if t[hk] else float("inf")
            print(
                "| "
                + " | ".join(
                    str(c).ljust(x)
                    for c, x in zip(
                        [
                            name if way == "seq" else "",
                            way,
                            f"{t[pk]:.1f}",
                            f"{t[hk]:.1f}",
                            f"{d:+.1f}",
                            f"{r:.2f}x",
                        ],
                        w,
                    )
                )
                + " |"
            )

    dispatch.shutdown()
    from lucen.execution import process_backend

    process_backend.shutdown()
    _tpool.shutdown()
    _ppool.shutdown()


if __name__ == "__main__":
    main()
