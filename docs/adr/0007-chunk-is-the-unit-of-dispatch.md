# 0007. The chunk is the unit of dispatch

## Context

A marked loop has an iteration space. The library could dispatch one task per
iteration, or one task per contiguous range of iterations (a chunk). Per-
iteration dispatch is the naive mapping and makes some analyses simpler, but it
pays the dispatch, serialization, audit, and commit cost once per iteration.
For a million-element loop that is a million times the overhead.

Every invariant Lucen holds constrains results, not the granularity of
dispatch. So the granularity is free to be chosen for performance.

## Decision

The chunk, a contiguous sub-range of the iteration space, is the single unit of
dispatch, serialization, audit, and commit. Codegen emits one chunk function
per block: a plain Python `for` loop over `[start, stop)`. All Lucen
overhead is paid once per chunk and amortizes toward zero per iteration.

The default chunk count is tuned per backend (more for threads, where dispatch
is cheap; fewer for processes, where a dispatch costs an order of magnitude
more), overridable with `chunks=`. A block must iterate a sized iterable,
because chunking requires a known length; this restriction is load-bearing, not
conservative.

## Consequences

- A zero-clause proven-safe block compiles to a plain loop plus positional
  stores, with no per-iteration interception. This is the property the
  zero-interception test enforces.
- Correctness reasoning happens at chunk granularity: disjointness is audited
  between chunks, commit is ordered by chunk.
- Unsized iterables cannot be chunked and take the sequential fallback. A user
  who wants a generator parallelized materializes it to a list first.

Spec: technical specification sections 5.7.1, 5.16.
