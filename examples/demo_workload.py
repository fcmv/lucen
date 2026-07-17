"""Runnable demo. Try:

    lucen explain examples/demo_workload.py
    lucen profile examples/demo_workload.py --per-block
"""
import math


def main():
    n = 200_000
    xs = list(range(n))
    ys = [0.0] * n

    # LUCEN START calibrate=false
    for i in range(len(xs)):
        ys[i] = math.sqrt(xs[i]) * 1.5 + math.sin(xs[i] * 0.001)
    # LUCEN END

    total = 0.0
    # LUCEN START
    for i in range(len(ys)):
        total += ys[i]
    # LUCEN END

    print(f"checksum: {total:.4f} (final i = {i})")


if __name__ == "__main__":
    main()
