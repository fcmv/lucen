# Glossary

The domain terms used across the Lucen documentation and code, in one place.

**Marked block.** A single `for` loop delimited by `# LUCEN START` and
`# LUCEN END`. The unit Lucen analyzes and may parallelize.

**Comment Invariant.** The guarantee that a file with Lucen not activated
runs identically to one where the pragmas were never written, because the
pragmas are ordinary comments.

**Chunk.** A contiguous sub-range of a loop's iteration space, and the unit of
dispatch, serialization, audit, and commit. All per-block overhead is paid once
per chunk (ADR 0007).

**Slab.** A chunk's private output buffer. Workers write only into their own
slab during execution; at join the slabs commit to the shared container in chunk
order (ADR 0008).

**Privatize-and-commit.** The execution model: private slabs during execution,
a disjointness audit at join, and an ordered commit. It makes the parallel
result bit-identical to sequential and gives exact container insertion order and
sequential-prefix error state.

**Sequential twin.** The plain-loop function codegen emits alongside the chunk
function. It is both the profitability probe and the fallback path, so the
sequential behavior is the user's original loop by construction.

**Write-set audit.** The join-time check that chunks wrote disjoint locations.
Tier A is a proven-contiguous lease, tier B a proven-disjoint assumption, and
tier C an asserted disjointness verified by a runtime bitmap. A conflict
discards the parallel attempt and re-runs sequentially.

**Wavefront.** The level-synchronous scheduler for a recognized-DAG block: the
iteration space is processed in levels (level `k` is the indices at
dependency-depth `k`), levels run in order with a barrier between them, and no
task waits on another task (ADR 0009).

**Reduction fold.** Combining per-element contributions into an accumulator. The
default `sequential_equivalent` order folds in the exact sequential order, so a
parallel float reduction is bit-identical to sequential (ADR 0006).

**Profitability gate.** The mechanism that declines to parallelize a loop that
would lose to dispatch overhead: a static pre-screen plus a runtime probe that
does real work while measuring (ADR 0010). Reports `PARALLEL_UNPROFITABLE` when
it declines.

**Probe.** The runtime measurement of per-iteration cost, run as a sequential
prefix over chunk 0 so it is real work, never wasted or repeated.

**Purity proof.** The static analysis that reads a helper's source and proves
whether it mutates hidden state. A proven-impure helper makes its block run
sequentially; anything unprovable keeps the documented trust (ADR 0003).

**Backend.** Where a block's chunks run: `process` (default for compute),
`thread` (by-reference blocks and free-threaded heavy compute), or `sequential`.
Selected by data shape, not by interpreter (ADR 0013).

**Fallback report.** The structured record of every block that ran sequentially
or downgraded, with its error, file, line, and reason. Read with
`lucen.get_fallback_report()`.

**Native core.** The optional Rust extension (`lucen._core`) that runs the
write-set audit and reduction fold. Every native operation has an
identical-semantics pure-Python twin, so it is never required for correctness.

**abi3 wheel.** The single native wheel, built against Python's stable ABI, that
loads on every locked CPython from 3.9 through 3.14. Free-threaded and other
interpreters install the pure-Python wheel instead.
