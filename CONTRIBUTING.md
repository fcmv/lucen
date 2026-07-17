# Contributing to Lucen

Thank you for considering a contribution. Lucen is a correctness-first
parallelizing compiler for Python, and its contribution bar is set by that:
the guarantee that a parallel run is bit-identical to plain sequential Python
is not negotiable, and the suite that proves it is the gate every change
passes through. This document explains how to get set up, what the bar is, and
how to get a change merged.

Before contributing code, please also read:

- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), which governs all project spaces.
- [AI_USAGE.md](AI_USAGE.md), the policy for AI-assisted contributions.
- The [engineering guide](docs/implementation/lucen_engineering_doc.md),
  which maps the codebase, and the
  [technical specification](docs/spec/lucen_technical_spec.md), which is the
  authority on every semantic.

## Getting set up

Lucen is a Python package with an optional Rust acceleration core. You can
develop the whole library in Python alone; the Rust toolchain is only needed to
build or change the native core.

```bash
git clone https://github.com/fcmv/lucen
cd lucen
pip install -e ".[dev]"
pytest
```

The full suite runs in a few seconds. If you have a Rust toolchain, the native
core is built into the editable install by maturin; without one, Lucen uses
its pure-Python fallback, which is fully supported and passes the same suite.

To build the native core explicitly after changing it:

```bash
cd lucen_core
cargo build --release
```

and copy the built library into `lucen/` as the extension module, or
re-run the maturin develop build.

## The contribution bar

Every change is measured against three properties, in this order.

### 1. Correctness is bit-identical

A change touching the execution pipeline must keep every workload bit-identical
across the sequential, thread, and process backends, and identical to the same
file run with Lucen not activated. This is not a code-review opinion; it is
an executable suite. Run it both ways:

```bash
pytest                                  # native path (if the core is built)
LUCEN_DISABLE_NATIVE=1 pytest        # pure-Python fallback path
```

The fallback run matters as much as the native run. Every native operation has
a pure-Python twin that must return exactly the same value, and CI runs the
suite under `LUCEN_DISABLE_NATIVE=1` for exactly this reason. A change that
passes on one path and not the other is not done.

### 2. Routing changes come with evidence

A change to backend selection, the cost model, or dispatch must show, with a
benchmark, that it does not regress. The guard is:

```bash
python tests/benchmarks/routing_check.py
```

which times every workload on every backend and asserts the gate picks the
fastest, and that every backend is bit-identical to plain Python. A routing
change that does not keep both properties is a regression, regardless of how
good the idea is. Improving a number is welcome; regressing one needs a stated,
accepted reason.

### 3. Additions to the native core must be measured

The native core holds only operations that are measured faster than their
Python twin on representative data and that pass the parity tests proving they
return exactly what the twin returns. Two operations were built, measured
slower, and deliberately kept in Python, with the measurement recorded in the
code so nobody re-attempts them blind. If you add to the core, bring the
benchmark that justifies it and the parity test that constrains it.

## Two review checkpoints

Most of the codebase is ordinary Python and reviews like ordinary Python. Two
areas carry invariants that are easy to break by accident and get explicit
attention in review:

- **The dependency-shape vocabulary** (`analyzer.py`). A change to shape
  recognition or normalization must preserve level-decomposability for any DAG
  form: every dependency index provably at or below `i // c` for constant
  `c > 1` (spec 5.5.3, 5.8). This is the property that lets the wavefront run
  without a blocking scheduler. State in the pull request why your change
  preserves it.
- **The codegen hot path** (`codegen.py`). A change to code generation or any
  per-iteration path must not add interception to the zero-clause proven-safe
  block (spec 5.16, rule 6). The zero-interception test asserts this on the
  generated code, not just on timing. Name the change's effect on it.

## Code style

- Match the surrounding code. The codebase favors precise, minimal comments in
  the places a reader genuinely needs them, and no narration where the code
  already says it. Docstrings are reserved for public API surface.
- Keep changes focused. A pull request that mixes a behavior change with a
  large unrelated refactor is hard to review against the correctness bar.
- No stubs, no placeholder implementations, and no `TODO`-shaped shortcuts in
  merged code. If something is deferred, it belongs in
  [ROADMAP.md](ROADMAP.md), not in a comment.

## Commits and pull requests

- Write commit messages in the imperative mood, and explain the *why*, not just
  the *what*. A message for a pipeline change should name the invariant or the
  benchmark it affects.
- Do not add tooling attribution trailers to commits (see
  [AI_USAGE.md](AI_USAGE.md) for the AI-assistance policy).
- Open an issue first for anything nontrivial, so the approach can be discussed
  before you invest in it. Small fixes can go straight to a pull request.
- A pull request should describe what changed, how it was verified (which of
  the three bar properties it exercises), and any routing or benchmark numbers
  it moved.

## Licensing of contributions

Lucen is licensed under Apache-2.0 (see [LICENSE](LICENSE)). Unless you
state otherwise, a contribution you submit for inclusion is offered under the
same license, per section 5 of that license. You retain copyright to your
contribution; you grant the project the rights the license describes.

## Reporting bugs and security issues

- A functional bug (including any case where a parallel run differs from
  sequential in a way not covered by the documented trust contract) goes in a
  public issue with a minimal reproduction.
- A suspected security issue, especially a silent wrong result reachable from
  ordinary code, goes through the private channel in
  [SECURITY.md](SECURITY.md), not a public issue.
