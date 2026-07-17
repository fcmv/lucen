import argparse
import json
import sys


def _speedups(data):
    out = {}
    for name, wl in data["workloads"].items():
        ms = wl["ms"]
        proc = ms.get("plx_proc")
        seq = ms.get("plx_seq")
        if proc and seq and proc > 0:
            out[name] = seq / proc
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline")
    ap.add_argument("current")
    ap.add_argument("--tolerance", type=float, default=0.30)
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    baseline = json.load(open(args.baseline, encoding="utf-8"))
    current = json.load(open(args.current, encoding="utf-8"))

    correctness_failures = []
    for name, wl in current["workloads"].items():
        for pathway, ok in wl["correct"].items():
            if pathway.startswith("plx") and not ok:
                correctness_failures.append(f"{name}/{pathway}")

    base_sp = _speedups(baseline)
    cur_sp = _speedups(current)
    regressions = []
    print(f"{'workload':22} {'baseline':>10} {'current':>10} {'change':>9}")
    for name in sorted(base_sp):
        if name not in cur_sp:
            continue
        b, c = base_sp[name], cur_sp[name]
        change = c / b - 1
        flag = "  REGRESSED" if change < -args.tolerance else ""
        print(f"{name:22} {b:9.2f}x {c:9.2f}x {change * 100:+8.1f}%{flag}")
        if change < -args.tolerance:
            regressions.append((name, b, c))

    print()
    if correctness_failures:
        print("CORRECTNESS REGRESSION:", ", ".join(correctness_failures))
        return 1
    if regressions:
        msg = "SPEEDUP REGRESSION beyond tolerance: " + ", ".join(n for n, _, _ in regressions)
        print(msg)
        return 1 if args.strict else 0
    print("no correctness or speedup regressions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
