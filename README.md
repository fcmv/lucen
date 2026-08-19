# Lucen

[![PyPI](https://img.shields.io/pypi/v/lucen.svg)](https://pypi.org/project/lucen/)
[![Python](https://img.shields.io/badge/python-3.9%20to%203.14t-blue.svg)](#supported-interpreters)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
[![CI](https://github.com/fcmv/lucen/actions/workflows/ci.yml/badge.svg)](https://github.com/fcmv/lucen/actions/workflows/ci.yml)

Lucen parallelizes ordinary Python loops that you mark with two comments. It
rewrites a marked loop into chunked parallel execution only when it can prove
the result will be identical to running the loop sequentially; anything it
cannot prove runs as the Python you wrote.

![Running a marked loop under lucen run: same output, 2.5x faster](assets/lucen.gif)

```python
# LUCEN START
for i in range(len(records)):
    scores[i] = score(records[i])
# LUCEN END
```

```bash
lucen run work.py
```

Over 100,000 records that loop goes from 5.7 s to 2.3 s on twelve cores, with
a bit-identical checksum ([examples/scored_records.py](examples/scored_records.py),
median of 3). No pools, no worker functions, no pickling code. The pragmas are
comments: delete the two lines, or uninstall Lucen, and you have the program
you started with.

## What Lucen promises

1. **Never an incorrect result.** Chunks write private slabs, audited for
   disjointness at join and committed in chunk order. Dict insertion order,
   float reduction bits and mid-error container state match sequential
   execution bit for bit. A write conflict discards the parallel attempt and
   re-runs the loop sequentially.
2. **Never disruptive.** What Lucen cannot prove runs sequentially, and the
   reason lands in `lucen.get_fallback_report()`. Exceptions keep their type,
   their message and the exact sequential-prefix state of your containers.
3. **Never silently pointless.** A static pre-screen plus a runtime probe
   refuses to parallelize loops that would lose to dispatch overhead, and
   reports that too.

The correctness claim is checked by a matrix of 7 interpreters x 8 workloads x
4 execution pathways, every cell bit-identical to plain Python
([BENCHMARK.md](BENCHMARK.md)).

## Installation

```bash
pip install lucen
```

Python 3.9 or later. On GIL builds pip installs a native Rust core (abi3, one
binary per platform) that runs the write-set audit and the reduction folds. On
free-threaded builds, where the abi3 core cannot load, pip installs the
pure-Python wheel instead; that path is fully supported and passes the same
test suite.

From source, with the optional Rust toolchain:

```bash
git clone https://github.com/fcmv/lucen
cd lucen
pip install -e ".[dev]"
pytest
```

### Supported interpreters

| Interpreter | Status | Native core |
|---|---|---|
| CPython 3.9 to 3.14 (GIL) | Supported, tested per release | yes |
| CPython 3.13t / 3.14t (free-threaded) | Supported, tested | pure-Python fallback |
| PyPy 3.11 | Supported, tested on the fallback | pure-Python fallback |
| GraalPy | Best-effort, tested on the fallback | pure-Python fallback |

## Usage

For a script you launch directly, use `lucen run`. It rewrites the marked
loops in the file you point at, then executes it:

```bash
lucen run work.py
```

Plain `python work.py` cannot parallelize a loop in the entry script: by the
time anything of Lucen's runs, that module is already compiled. For an
application you start yourself, install the import hook before the module
holding the marked loop is imported:

```python
import lucen
lucen.activate()

import work
work.main()
```

On Windows and macOS the process backend re-imports the entry module in every
worker, so it needs the usual `if __name__ == "__main__":` guard. Lucen detects
a missing guard in the parent and runs the block sequentially with an
actionable message, rather than letting `multiprocessing` fail in the children.

## Seeing what it decided

`lucen explain` is static, and reports facts as facts:

```
$ lucen explain examples/demo_workload.py
examples/demo_workload.py: 2 marked block(s) [gil interpreter assumed]

Block 1 (line 14)
  + Parallelized
  Backend: PROCESS, flat chunk scheduler
  Reason: all shared writes are provably or assertedly disjoint per iteration
  Clauses in effect: calibrate=false
  Runtime-dependent (never reported statically): argument picklability,
  custom-callable well-formedness, pool availability - see `lucen profile`.
```

A block it refuses names the reason:

```
Block 1 (line 12)
  - Sequential
  Reason: cross-iteration dependency 'scores[i - 1]' (monotonic chain)
```

`lucen profile script.py --per-block` reports what actually ran, per block,
with timings. A runtime downgrade prints one line on stderr and is retained as
a structured record:

```
lucen fallback: PARALLEL_UNPROFITABLE (work.py:4): measured ~44 ns/iteration
loses to dispatch overhead; ran SEQUENTIAL (calibrate=false overrides, spec 5.17)
```

```python
for record in lucen.get_fallback_report():
    print(record.error, record.file, record.line, record.message)
```

In CI, `lucen explain --strict --baseline baseline.json` fails the build when a
block's classification regresses against a committed baseline, so a refactor
that quietly de-parallelizes a hot loop is caught in review.

## Tuning

Clauses go on the `# LUCEN START` line. Each one trades a Lucen-held proof for
an assertion of yours, or exactness for speed, never a different answer. A
malformed clause is a loud import-time error with a did-you-mean suggestion.

```python
# LUCEN START calibrate=false, timeout=5.0, on_error=collect
```

| Clause | What it does |
|---|---|
| `backend=` | Pin the backend: `thread`, `process`, `sequential`, with `pool_size`/`chunks` |
| `calibrate=` | Control the profitability gate (`false` forces parallel) |
| `grainsize=` | Level width for a recognized-DAG wavefront |
| `affinity=` | CPU affinity: `compact`, `scatter`, or `explicit(cores=[...])` |
| `nested=` | Policy for a block reached inside another parallel block |
| `depend=` | Assert independence (`none`) or an acyclic order |
| `skip_runtime_check=` | Disable the runtime write-set audit (with `depend=none`) |
| `trust=` | Waive the purity or pickle check: `callables`, `pickle`, `all` |
| `reduce=` | Name the reduction op (`sum`, `min`, ...) or `custom(fn=, identity=)` |
| `reduction_order=` | `sequential_equivalent` (default, bit-identical), `stable`, or `custom` |
| `timeout=` | Bound wall time; raises `ParallelTimeoutError` |
| `on_error=` | Gather per-iteration exceptions instead of failing fast (`collect`) |
| `strict=` | Turn this block's fallbacks into hard errors |
| `on_fallback=` | Set how a fallback is surfaced for this block |
| `progress=` | Per-chunk or per-iteration progress reporting |

Every accepted form is in [docs/pragmas.md](docs/pragmas.md). Project-wide
defaults and hard ceilings (pool sizes, timeout ceilings, an
experimental-features veto) live in `lucen.toml` at your project root.

Where the purity proof cannot read a helper's source (C extensions, dynamic
dispatch), `# LUCEN TRUST` above its `def` asserts it is parallel-safe;
`trust=callables` does the same per block and `[trust] callables` project-wide.
`depend=none` asserts your indexed writes are disjoint, and the runtime audit
still catches you if they are not; it takes `skip_runtime_check=true` on top of
that, two deliberate assertions, to reach a wrong result.

Experimental features are off by default and enabled per process:

```python
lucen.activate(experimental=["early_exit", "typed_buffers"])
```

| Flag | Effect |
|---|---|
| `early_exit` | Parallelize loops containing `break` with exact first-match semantics |
| `typed_buffers` | Dense array-output maps ship typed result slabs on PROCESS |
| `branch_sensitive_deps` | Per-branch dependency classification under the runtime audit |

## How it works

```
scanner -> rewriter -> selector -> codegen -> dispatch
(pragmas)  (classify)  (route)     (twins)    (execute + audit + commit)
```

The rewriter classifies every name in the block (loop-local, read-only,
reduction accumulator, indexed write, cross-iteration read) and recognizes
dependency shapes analytically, including `results[i // 2]`-style DAGs. The
selector routes each block, with reasons. Codegen emits a chunk function for
workers and a sequential twin that is also the fallback path, so the sequential
behavior *is* your original loop. Dispatch runs chunks over persistent pools,
audits write disjointness at join, folds reductions in exact element order, and
commits in chunk order.

Backend selection is static and interpreter-independent. Maps and reductions go
to PROCESS on both GIL and free-threaded builds, because shared-object
reference counting makes threads lose on shared-container workloads even
without a GIL. THREAD serves by-reference blocks and free-threaded heavy
compute; everything lighter stays sequential. The loop bodies themselves are
always your Python: compiling eligible bodies to native kernels is the flagship
roadmap item, not a current feature. See
[docs/architecture.md](docs/architecture.md) and the
[technical specification](docs/spec/lucen_technical_spec.md).

## Performance

Medians of 5 after warm-up on a 12-core i5-12450HX, across seven interpreters.
"Native Python" is the identical file with the pragmas treated as what they
are, comments; "Lucen" is the shipped product with its gate deciding.

![Lucen speedup by workload, CPython 3.11 on 12 cores](assets/benchmark.svg)

| Workload | Native Python (ms) | Lucen, gate on (ms) | Best hand-written (ms) |
|---|---|---|---|
| light map (1M) | 67.7 to 93.3 | 32.1 to 50.4 | 48.8 |
| medium map (40k) | 102.9 to 138.1 | 30.1 to 44.0 | 24.1 |
| heavy map (20k) | 1028.8 to 1258.0 | 282.5 to 401.1 | 244.0 |
| light reduction (1M) | 54.1 to 77.6 | 29.4 to 35.8 | 27.6 |
| heavy reduction (20k) | 1028.0 to 1273.0 | 284.4 to 412.2 | 243.2 |
| recognized DAG (100k) | 10.4 to 14.4 | 6.6 to 9.2 | 7.8 |
| buffer map (1M, array) | 75.9 to 106.3 | 52.8 to 67.1 | 26.6 |
| nested heavy (4k) | 61.6 to 79.0 | 16.6 to 27.4 | 13.7 |

Where parallelism pays, Lucen runs 3x to 4.3x faster than its own sequential
execution and lands within a few percent of hand-tuned `concurrent.futures`
code. Where it cannot pay, the gate stays sequential at effectively zero
overhead. The hand-written comparison code was AI-generated to an expert
standard, and its parallel float reductions produce different bits than
sequential Python on every interpreter tested, which is the asterisk on those
last two columns; Lucen's reductions are bit-identical everywhere.

Every number, pathway, interpreter and the raw JSON: [BENCHMARK.md](BENCHMARK.md).

## Limitations

- **Helper purity is proven only where the source is readable.** A stateful C
  extension or dynamically dispatched callable keeps the documented trust and
  can diverge per worker.
- **`typed_buffers` is not in the cost model.** Array-output maps route
  sequential unless you force `backend=process` with the flag on.
- **Light reductions carry a 5 to 10 percent probe overhead**, because
  reductions cannot use the twin-probe fast path yet.
- **The recognized-DAG wavefront runs sequentially by default.** Its parallel
  form pays off only on free-threaded builds under `backend=thread`.
- **No native core on free-threaded builds**; the pure-Python fallback covers
  them.
- **One loop or comprehension per pragma pair**, no `async` bodies, and
  generator expressions are never parallelized.

The full inventory, including the exact boundary of the correctness guarantee
and what two red-team campaigns (130+ adversarial scenarios) found there, is in
[LIMITATIONS.md](LIMITATIONS.md). Planned work against each item is in
[ROADMAP.md](ROADMAP.md).

## Documentation

| Document | Contents |
|---|---|
| [Architecture](docs/architecture.md) | The pipeline and dispatch flow, with diagrams |
| [Pragma and clause reference](docs/pragmas.md) | Every pragma and clause, with accepted forms |
| [Glossary](docs/glossary.md) | The domain terms in one place |
| [Technical specification](docs/spec/lucen_technical_spec.md) | Every semantic and invariant |
| [Engineering guide](docs/implementation/lucen_engineering_doc.md) | How the code is organized |
| [Formal specifications](docs/formal/) | TLA+ and executable checks of the concurrency invariants |
| [Paper](docs/paper/lucen.md) | The design and evaluation, in preprint form |
| [LIMITATIONS.md](LIMITATIONS.md) | Known gaps and the trust contract |
| [ROADMAP.md](ROADMAP.md) | What is planned, in what order |
| [STABILITY.md](STABILITY.md) | What is stable and what may change |
| [examples/](examples/) | Runnable examples, including the spec's worked DAG |

## Contributing

Start with `pip install -e ".[dev]" && pytest`. Changes to the execution
pipeline are judged by the invariant suite, and no routing change lands without
benchmark evidence. See [CONTRIBUTING.md](CONTRIBUTING.md) for the process,
[AI_USAGE_GUIDELINE_FOR_PR.md](AI_USAGE_GUIDELINE_FOR_PR.md) for AI-assisted
contributions, [GOVERNANCE.md](GOVERNANCE.md) for how decisions are made, and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards. Security
issues go through [SECURITY.md](SECURITY.md), not public issues.

## License

Apache-2.0. See [LICENSE](LICENSE).
