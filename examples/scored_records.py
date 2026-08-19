"""CPU-bound map over 100,000 records. Try:

    python examples/scored_records.py
    lucen run examples/scored_records.py
"""
import math
import time


def score(x):
    acc = 0.0
    for k in range(400):
        acc += math.sin(x * 0.001 + k) * math.cos(k * 0.5)
    return acc


def main():
    records = list(range(100_000))
    scores = [0.0] * len(records)
    started = time.perf_counter()

    # LUCEN START
    for i in range(len(records)):
        scores[i] = score(records[i])
    # LUCEN END

    print(f"checksum: {sum(scores):.6f}")
    print(f"elapsed: {time.perf_counter() - started:.2f} s")


if __name__ == "__main__":
    main()
