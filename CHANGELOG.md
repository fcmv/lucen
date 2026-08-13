# Changelog

All notable changes to Lucen are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- A marked block calling `open()` is downgraded to sequential, as the purity
  contract always intended. `builtins.open` reports `__module__ == "io"`, so the
  `open` entry in the impure-builtin set never matched and a block reading or
  writing a file kept its parallel routing. The match is now on the object the
  name resolves to. Every other name in the set behaves as before.

### Internal

- The nightly mutation job mutates both accel modes. Pinning
  `LUCEN_DISABLE_NATIVE` made the accelerated half dead code, so every mutant of
  `_accel.py` and of the accelerated branch in `audit_disjoint_dict_slabs`
  reported as a survivor no test could have killed. A mutant is a real survivor
  only where it survived in both lanes.
- The interrupt teardown, the calibration chunk probe, the commit prefix, the
  SKIP marker, the buffer slice store and the paths a probe leaves behind are
  covered. `_Plan.element_at` had never run, because `early_exit` carried a
  byte-identical private copy that shadowed it; the copy is gone. `_pool_size`
  was assigned in two places and read in none, and is gone.

## [1.2.0] - 2026-08-12

Comprehensions join the loop as a markable construct. A `# LUCEN START` pair
may now wrap a comprehension assignment directly, with the same guarantee the
loop has always carried: the parallel result is bit-identical to plain Python.

### Added

- Marked list, dict, and set comprehensions are parallelized, including filters
  and further `for` clauses. Each form builds one positional list over the
  outermost iterable and is rebuilt sequentially afterwards, so results,
  including dict and set iteration order, match plain Python exactly.
- `total = sum(elt for t in it)` is parallelized. Its elements are computed in
  parallel into one positional list and `sum` itself adds them afterwards, so
  the total is bit-identical to the builtin's, including the compensated float
  summation CPython 3.12 and newer use. A bare generator expression is lazy and
  is never parallelized.
- `LUCEN_DISABLE_CACHE=1` bypasses the rewrite cache. It is for work on Lucen's
  own codegen: the cache key includes the Lucen version, which does not move
  between edits to a checkout, so a stale rewrite would otherwise be served as
  current.

### Documentation

- `LIMITATIONS.md` records two behaviours that shipped without an entry: an
  exception that cannot be rebuilt from its message degrades to `RuntimeError`
  when it crosses the process boundary, and its traceback does not survive; and
  a process pool on a spawn platform costs one import graph per worker, so peak
  memory scales with the worker count rather than the data.
- `RELEASING.md` now lists five version fields rather than three. The
  `lucen_core/Cargo.lock` entry and `CITATION.cff` were undocumented, so a
  release following the old instructions shipped a stale lock and a citation
  record naming the previous version.

### Internal

- `tools/mutation_report.py` runs mutation testing over the correctness core and
  reports the survivors with their diffs. It runs the suite once per accel mode,
  because pinning one mode makes the other dead code and its mutations report as
  false survivors. A non-gating nightly CI lane runs the same thing.
- Test coverage for deep attribute paths and the contiguous-coverage audit in
  the native accelerator.
- The generated `assets/` SVGs and PNGs are ASCII-only.
- Mutation-testing artifacts are ignored, and two comments were trimmed to the
  house length.
- The `sum` support above was corrected before release: the first
  implementation folded `+` over the elements, which is not what `sum` does on
  CPython 3.12 and newer. No released version carried it.
- The profitability gate's tests feed the probe a measurement instead of timing
  one chunk on the clock. That timing is a few microseconds, so one preemption
  on a shared runner routed a tiny block parallel and failed the suite. The
  profitable half of the gate is now covered as well; every other test that
  asserts a parallel run forces the backend with `calibrate=false`, which skips
  the gate entirely.

### Dependencies

- `pyo3` 0.29.0 to 0.29.1 (#8).

## [1.1.1] - 2026-08-04

### Fixed

- `lucen run` now executes the target script as the real `__main__` module. The
  spawn-safety scan previously inspected Lucen's own launcher, found no
  `if __name__ == "__main__"` guard, and refused the process backend, so marked
  loops ran sequentially on spawn platforms (#4).
- A loop whose `range` or `enumerate` header is rebound in the module is now
  declined. Codegen substitutes builtin semantics for those headers, so a
  rebound name could produce a result that differed from sequential Python (#5).
- An interrupted parallel block now drains its running chunks before unwinding,
  instead of leaving workers writing into the caller's containers (#6).
- Failure to start a worker falls back to a reported sequential run instead of
  raising partway through the loop (#7).
- Release notes are built from this changelog. The published notes previously
  showed the commit subject, because the tag annotation is not present in the
  checkout the release job runs from.

## [1.1.0] - 2026-07-19

### Added

- `lucen run <script>` runs a script with Lucen active, rewriting the marked
  loops in the script itself. A single self-contained file now parallelizes
  without a separate importable entry module. Plain `python script.py` cannot do
  this, because the entry module is already compiled and running before
  `lucen.activate()` installs the import hook.

### Fixed

- `lucen profile` now parallelizes loops in modules the target script imports.
  It previously did not place the script's directory on `sys.path`, so an
  imported sibling module raised `ModuleNotFoundError`, and it rooted the import
  hook at the wrong directory, so imported modules were never rewritten.

## [1.0.0] - 2026-07-17

First public release.

Lucen is a source-to-source compiler that parallelizes ordinary Python `for`
loops marked with comment pragmas, under one guarantee that has no tier and no
opt-out: a parallel run is bit-identical to the same file executed as plain
sequential Python, floating point results and container insertion order
included.

### Added

- Comment-pragma surface: `# LUCEN START`, `# LUCEN END`, and `# LUCEN TRUST`.
  They are ordinary comments; a file with the pragmas stripped runs identically.
- A profitability gate that parallelizes only the loops it can prove are both
  safe and worthwhile. Everything else runs sequentially with no added overhead.
- Interpreter-independent backend routing. CPU-bound work is sent to processes
  on GIL builds and to threads on free-threaded builds, where threads
  parallelize without the pickling and subprocess cost.
- Sequential-equivalent reduction folds that reproduce the sequential floating
  point result bit for bit.
- Level-synchronous wavefront execution for loops with a recognized dependency
  structure.
- An optional native core (`lucen._core`) accelerating write-set audits and
  reduction folds, with an identical-semantics pure-Python fallback. Results
  never depend on whether the native core is present.
- The `lucen.toml` configuration schema, the `lucen explain` and
  `lucen profile` subcommands, and the public API (`lucen.activate`,
  `lucen.deactivate`, `lucen.get_fallback_report`).

### Supported

- CPython 3.9 through 3.14, including the free-threaded builds.
- Two wheels per release: a native `abi3` wheel that loads on every CPython 3.9
  and later GIL build from one binary per platform, and a `py3-none-any`
  pure-Python wheel for free-threaded interpreters and architectures without a
  native build. The install always succeeds and always runs correctly.

[1.0.0]: https://github.com/fcmv/lucen/releases/tag/v1.0.0
