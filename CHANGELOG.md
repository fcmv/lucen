# Changelog

All notable changes to Lucen are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

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
