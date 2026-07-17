"""The spec's own worked example (spec 9.2), runnable as-is:

    lucen explain examples/03_recognized_dag_reduction.py --assume-free-threaded
    lucen profile examples/03_recognized_dag_reduction.py

On a GIL build the PROCESS backend needs picklable helpers: a function
defined in the profiled script itself (__main__) is not importable by
workers, so this falls back to a reported sequential run. Move `combine`
into an importable module (or run on a free-threaded build) to see the
wavefront execute in parallel.
"""


def combine(parent, weight):
    return parent + weight * 2


def main():
    n = 100_000
    results = [1] + [0] * (n - 1)
    weights = list(range(n))

    # LUCEN START grainsize=64, progress=true
    for i in range(1, n):
        results[i] = combine(results[i // 2], weights[i])
    # LUCEN END

    print(f"results[-1] = {results[-1]}")


if __name__ == "__main__":
    main()
