<!--
Thank you for contributing to Lucen. Please read CONTRIBUTING.md if you have
not. The checklist below is the correctness bar; it is not a formality. A
parallelizing compiler's contributions are judged first on whether they hold the
guarantee that a parallel run is bit-identical to plain sequential Python.
-->

## What this changes

<!-- A clear description of the change and the problem it solves. Link the issue
it addresses, if any. -->

## Why

<!-- The reasoning. For a pipeline or routing change, name the invariant or the
benchmark it affects. -->

## How it was verified

<!-- Describe how you checked this, concretely. Not "tests pass" but which
properties you exercised. -->

## Checklist

- [ ] The suite passes on the **native path**: `pytest`.
- [ ] The suite passes on the **pure-Python fallback path**:
      `LUCEN_DISABLE_NATIVE=1 pytest`. (Both matter. A change that passes on
      one path and not the other is not done.)
- [ ] Output is **bit-identical** across the sequential, thread, and process
      backends, and identical to the same file with Lucen not activated, for
      any workload this change touches.
- [ ] If this changes **backend routing, the cost model, or dispatch**:
      `python tests/benchmarks/routing_check.py` still shows the gate picking
      the fastest backend on every workload, with no correctness regression.
      Attach the before/after numbers.
- [ ] If this adds to the **native core**: a benchmark shows it is faster than
      its pure-Python twin on representative data, and a parity test proves it
      returns exactly what the twin returns.
- [ ] If this touches `analyzer.py`'s **shape vocabulary**: the change preserves
      level-decomposability (every dependency index provably at or below
      `i // c` for constant `c > 1`), stated here with the reasoning.
- [ ] If this touches `codegen.py` or a **per-iteration path**: it adds no
      interception to the zero-clause proven-safe block (the zero-interception
      test still passes).
- [ ] No stubs, placeholder implementations, or `TODO`-shaped shortcuts in the
      merged code. Deferred work goes in ROADMAP.md.

<!-- If a box does not apply, leave it unchecked and say why in the section
above. An honest "not applicable, because ..." is expected; a checked box that
is not true is not. -->
