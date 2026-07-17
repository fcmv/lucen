# 0008. Privatize-and-commit replaces versioned cells

## Context

Multiple workers writing into a shared container concurrently must not corrupt
it, and the final state must equal what sequential execution would produce. One
approach is per-element versioned cells with a compare-and-set on every write,
which detects conflicts as they happen. That pays a synchronization cost on
every single write and leaves the container in a partially-updated state if a
worker fails midway.

## Decision

Each chunk writes into a private slab: a preallocated list for list output, a
chunk-local dict for keyed output, or per-chunk partial accumulators for
reductions. Inside the chunk function there is zero interception; a plain
`slab[j] = ...` stores at Python speed with no per-write bookkeeping. At join,
slabs commit to the real container in chunk index order, as slice-assignments
or ordered merges. Disjointness is verified once at join by a write-set audit,
not per write.

Because nothing is committed before the join, the commit is transactional by
construction. Any conflict or serialization failure discards the uncommitted
slabs and re-runs the block sequentially, with no partial state to roll back.

## Consequences

- Per-iteration write cost is zero for proven-safe blocks; the audit cost is
  paid once per chunk.
- Two guarantees are strengthened over per-write commits. Dict insertion order
  is bit-identical to sequential, because within-chunk order is preserved and
  chunks merge in order. And a mid-block error leaves the container in exactly
  the sequential-prefix state, because chunks below the failure commit and the
  rest are discarded, rather than leaving scattered partial writes.
- A false `depend=none` assertion produces a deterministic wrong result (reads
  see the pre-block state) rather than a racy one, which is at least
  reproducible. The audit still catches a single false assertion; it takes two
  explicit waivers to reach a silent wrong result.
- Slabs and the container coexist in memory at join. This transient cost is
  bounded by the output size and freed incrementally in chunk order.

Spec: technical specification sections 5.7.2, 5.7.3.
