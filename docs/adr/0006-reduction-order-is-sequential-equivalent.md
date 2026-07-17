# 0006. Reduction order defaults to sequential-equivalent

## Context

A parallel reduction can combine partial results in many orders. The fastest is
a tree-combine of per-worker partials. But floating-point addition is not
associative: `(a + b) + c` and `a + (b + c)` can differ in their last bits. A
tree-combine of partial sums therefore produces a different result than the
left-to-right fold that sequential Python performs.

For a library whose one guarantee is bit-identical results, a reduction that
returns different bits than sequential is a broken guarantee, even though the
answer is "numerically close."

## Decision

The default reduction order is `sequential_equivalent`: per-element
contributions are folded in the exact order the sequential loop would combine
them. Chunks are index-contiguous by construction, so the parent folds
per-chunk contributions in chunk order, which reproduces the sequential
left-to-right fold bit for bit. There is no partial-sum re-association in this
mode.

Two opt-in modes exist for users who explicitly want them and understand the
trade: `stable` permits a reproducible tree-combine (where combine parallelism
can win), and `custom` takes a user callable whose associativity is the user's
assertion. Both are declared, so choosing them is a conscious act, and they are
the only two documented exceptions to naive/expert output parity.

## Consequences

- A parallel float reduction is bit-identical to sequential, on every backend
  and every interpreter. This is verified continuously by the property suite,
  which compares float reductions bit-for-bit.
- The fold cost is proportional to the number of chunks, not the number of
  elements, so sequential-equivalent ordering is effectively free.
- Combine parallelism (a tree of partial sums) is unavailable by default. This
  is deliberate: it is an optimization that would break bit-identity, so it is
  opt-in behind `stable`.

Spec: technical specification section 5.12.
