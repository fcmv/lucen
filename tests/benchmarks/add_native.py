import json
import os
import statistics
import sys
import time

from bench import WORKLOADS

HERE = os.path.dirname(os.path.abspath(__file__))
REPS = 5


def measure(src, factory, n):
    time.sleep(1.0)
    e = factory(n)
    exec(src, e)
    ts = []
    for _ in range(REPS):
        e = factory(n)
        t0 = time.perf_counter()
        exec(src, e)
        ts.append(time.perf_counter() - t0)
    return round(statistics.median(ts) * 1000, 2)


def main():
    tag = sys.argv[1]
    natives = {}
    for name, n, src, factory in WORKLOADS:
        natives[name] = measure(src, factory, n)
        print(f"[{tag}] native {name}: {natives[name]} ms", flush=True)
    for sub in ("", "exp"):
        path = os.path.join(HERE, "results", sub, tag + ".json")
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        for name, ms in natives.items():
            d["workloads"][name]["ms"]["native"] = ms
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        print("patched", path)


if __name__ == "__main__":
    main()
