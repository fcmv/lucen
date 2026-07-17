import json
import statistics
import sys
import time
import types

import lucen.support.costmodel as cm
import lucen.execution.dispatch as D
from lucen.support import config
from lucen.execution import dispatch
from bench import WORKLOADS, build_spec, iterable_for
from bench_vs_hand import NCORE, hand_impls

REPS = 5
COOL = 1.0


def med(fn):
    time.sleep(COOL)
    fn()
    ts = []
    for _ in range(REPS):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    return round(statistics.median(ts) * 1000, 2)


def data_keys(env):
    return [k for k, v in env.items() if not callable(v) and not isinstance(v, types.ModuleType)]


def norm(v):
    if isinstance(v, (list, tuple)):
        return [norm(x) for x in v]
    if hasattr(v, "tolist"):
        return v.tolist()
    if isinstance(v, (bytearray, bytes)):
        return list(v)
    try:
        import array

        if isinstance(v, array.array):
            return list(v)
    except ImportError:
        pass
    return v


def golden(src, factory, n):
    g = factory(n)
    exec(src, g)
    return {k: norm(g[k]) for k in data_keys(g)}


def identical(env, gold, keys):
    return all(norm(env[k]) == gold[k] for k in keys)


def main():
    out_path = sys.argv[1]
    exp = len(sys.argv) > 2 and sys.argv[2] == "exp"
    flags = frozenset({"early_exit", "branch_sensitive_deps", "typed_buffers"})
    config.set_active(config.Config(experimental=flags if exp else frozenset()))
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    from lucen.execution import _accel

    real_prof = D._profitable
    real_static = cm.statically_unprofitable

    result = {
        "python": sys.version.split()[0],
        "gil": bool(gil),
        "native_accel": bool(_accel.ACCELERATED),
        "cores": NCORE,
        "reps": REPS,
        "experimental": sorted(flags) if exp else [],
        "workloads": {},
    }

    for name, n, src, factory in WORKLOADS:
        analysis, _, spec = build_spec(src)
        gold = golden(src, factory, n)
        keys = data_keys(factory(n))
        h_seq, h_thr, h_proc = hand_impls(name, n, factory)
        wl = {"n": n, "correct": {}, "ms": {}}

        for label, bk, forced in (
            ("plx_seq", "sequential", True),
            ("plx_thr", "thread", True),
            ("plx_proc", "process", True),
            ("plx_gate", None, False),
        ):
            if forced:
                D._profitable = lambda *a, **k: True
                cm.statically_unprofitable = lambda *a, **k: False
            env = factory(n)
            dispatch.reset_runtime_state()
            D.execute(spec, iterable_for(analysis, env), env, force_backend=bk)
            wl["correct"][label] = identical(env, gold, keys)
            if label == "plx_gate":
                st = list(dispatch.get_block_stats().values())[-1]
                wl["gate_backend"] = st["backend"]
            D._profitable = real_prof
            cm.statically_unprofitable = real_static

        def hand_outputs(fn):
            r = fn()
            return norm(r)

        def golden_hand():
            return hand_outputs(h_seq)

        gh = golden_hand()
        wl["correct"]["hand_seq"] = True
        wl["correct"]["hand_thr"] = hand_outputs(h_thr) == gh
        wl["correct"]["hand_proc"] = hand_outputs(h_proc) == gh

        env = factory(n)
        it = iterable_for(analysis, env)
        scalars = [k for k, v in env.items() if isinstance(v, (int, float)) and k != "n"]
        zero = {k: type(env[k])() for k in scalars}

        def plx(bk, forced=True):
            def run():
                for k, z in zero.items():
                    env[k] = z
                if forced:
                    D._profitable = lambda *a, **k2: True
                    cm.statically_unprofitable = lambda *a, **k2: False
                dispatch.reset_runtime_state()
                D.execute(spec, it, env, force_backend=bk)
                D._profitable = real_prof
                cm.statically_unprofitable = real_static

            return run

        wl["ms"]["plx_seq"] = med(plx("sequential"))
        wl["ms"]["plx_thr"] = med(plx("thread"))
        wl["ms"]["plx_proc"] = med(plx("process"))
        wl["ms"]["plx_gate"] = med(plx(None, forced=False))
        wl["ms"]["hand_seq"] = med(h_seq)
        wl["ms"]["hand_thr"] = med(h_thr)
        wl["ms"]["hand_proc"] = med(h_proc)

        result["workloads"][name] = wl
        print(f"[{result['python']}{'t' if not gil else ''}] done: {name}", flush=True)

    dispatch.shutdown()
    from lucen.execution import process_backend

    process_backend.shutdown()
    import bench_vs_hand

    bench_vs_hand._tpool.shutdown()
    bench_vs_hand._ppool.shutdown()

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    print("wrote", out_path)


if __name__ == "__main__":
    main()
