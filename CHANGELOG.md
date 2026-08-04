# Changelog

All notable changes to Lucen are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

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
