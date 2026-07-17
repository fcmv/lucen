from __future__ import annotations

import argparse
import json
import os
import pickle
import platform
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor


def _noop(payload=None):
    return payload


def _median_ns(samples):
    return int(statistics.median(samples))


def timer_floor_ns(reps: int = 5000) -> int:
    deltas = []
    for _ in range(reps):
        a = time.perf_counter_ns()
        b = time.perf_counter_ns()
        deltas.append(b - a)
    return _median_ns(deltas)


def thread_dispatch_ns(workers: int, reps: int = 2000) -> dict:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pool.submit(_noop).result()
        single = []
        for _ in range(reps):
            t0 = time.perf_counter_ns()
            pool.submit(_noop).result()
            single.append(time.perf_counter_ns() - t0)
        batch = []
        for _ in range(50):
            t0 = time.perf_counter_ns()
            futures = [pool.submit(_noop) for _ in range(32)]
            for f in futures:
                f.result()
            batch.append((time.perf_counter_ns() - t0) // 32)
    return {"single_roundtrip_ns": _median_ns(single), "batch32_per_chunk_ns": _median_ns(batch)}


def process_costs(reps: int = 200) -> dict:
    t0 = time.perf_counter_ns()
    pool = ProcessPoolExecutor(max_workers=2)
    pool.submit(_noop, 1).result()
    spawn_ns = time.perf_counter_ns() - t0
    single = []
    for _ in range(reps):
        t0 = time.perf_counter_ns()
        pool.submit(_noop, 1).result()
        single.append(time.perf_counter_ns() - t0)
    payload = list(range(10_000))
    payload_ns = []
    for _ in range(50):
        t0 = time.perf_counter_ns()
        pool.submit(_noop, payload).result()
        payload_ns.append(time.perf_counter_ns() - t0)
    pool.shutdown()
    return {
        "spawn_ms_per_worker": spawn_ns / 2 / 1e6,
        "warm_roundtrip_us": _median_ns(single) / 1e3,
        "roundtrip_10k_int_payload_us": _median_ns(payload_ns) / 1e3,
    }


def pickle_throughput() -> dict:
    blob = b"x" * (8 << 20)
    out = {}
    for proto in (4, 5):
        t0 = time.perf_counter_ns()
        for _ in range(10):
            pickle.dumps(blob, protocol=proto)
        ns = (time.perf_counter_ns() - t0) / 10
        out[f"p{proto}_mb_s"] = round((8 / (ns / 1e9)), 1)
    buffers = []
    view = pickle.PickleBuffer(bytearray(blob))
    t0 = time.perf_counter_ns()
    for _ in range(10):
        buffers.clear()
        pickle.dumps(view, protocol=5, buffer_callback=buffers.append)
    ns = (time.perf_counter_ns() - t0) / 10
    out["p5_oob_mb_s"] = round((8 / (ns / 1e9)), 1)
    return out


def commit_throughput() -> dict:
    n = 1_000_000
    container = list(range(n))
    slab = list(range(n // 4))
    t0 = time.perf_counter_ns()
    for _ in range(20):
        container[0 : len(slab)] = slab
    slice_ns = (time.perf_counter_ns() - t0) / 20
    src = {i: i for i in range(200_000)}
    t0 = time.perf_counter_ns()
    for _ in range(20):
        {}.update(src)
    dict_ns = (time.perf_counter_ns() - t0) / 20
    return {
        "list_slice_assign_melem_s": round(len(slab) / (slice_ns / 1e9) / 1e6, 1),
        "dict_bulk_update_melem_s": round(len(src) / (dict_ns / 1e9) / 1e6, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="tests/benchmarks/budgets.json")
    args = parser.parse_args()
    workers = os.cpu_count() or 4
    gil_probe = getattr(sys, "_is_gil_enabled", None)
    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "free_threaded": bool(gil_probe and not gil_probe()),
        "cpu_count": workers,
        "timer_floor_ns": timer_floor_ns(),
        "thread": thread_dispatch_ns(workers),
        "process": process_costs(),
        "pickle": pickle_throughput(),
        "commit": commit_throughput(),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
