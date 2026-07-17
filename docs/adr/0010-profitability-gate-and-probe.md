# 0010. Profitability gate with a sequential-prefix probe

## Context

Not every parallel-eligible loop should run in parallel. A trivial body over a
small input loses to dispatch overhead: shipping the work to workers costs more
than the work saves. A parallelizer that dispatches such loops makes programs
slower, which violates the never-silently-pointless goal even though the result
stays correct.

Deciding profitability needs a cost estimate. A static estimate alone is too
crude for the boundary cases; a runtime measurement is accurate but must not
waste work or change behavior.

## Decision

Two estimators and a memo, all invisible to output.

A static pre-screen, computed once and cached, catches the hopeless cases in
`explain` before any run. It only classifies a block unprofitable when it fails
by a wide margin, so it is never wrong at the boundary.

A runtime probe measures the real per-iteration cost by running chunk 0. The key
property: the probe is a sequential prefix. Chunk 0 runs in the caller's thread
exactly as plain Python would run it, same order and same effects, so the probe
is real work that also serves as the timing measurement, never wasted or
repeated. If the projected parallel time beats sequential, the remaining chunks
dispatch and chunk 0's result is committed as chunk 0. Otherwise the block
finishes sequentially and reports `PARALLEL_UNPROFITABLE`. A pure map is probed
on its in-place sequential twin, so the probe chunk needs no slab or commit.

The decision is memoized per block and refreshed on a call count or an
iteration-count regime change. A wrong prediction costs at most about one chunk
of suboptimal scheduling per cycle, and it never changes output.

## Consequences

- A too-small loop runs sequentially at full speed instead of being made slower.
- The gate is a bounded-cost estimator, not a promise. This is stated plainly:
  Lucen does not guarantee a speedup, it guarantees it will not silently make
  things much worse, and it reports when it declines.
- `calibrate=false` forces the parallel path for a user who knows better than
  the gate for a specific block. Every `calibrate=` tier produces identical
  output; the clause trades time and observability only.

Spec: technical specification section 5.17.
