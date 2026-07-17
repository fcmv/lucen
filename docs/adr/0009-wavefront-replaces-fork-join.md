# 0009. Level-synchronous wavefront replaces fork-join

## Context

A recognized-DAG block (`results[i] = combine(results[i // c], ...)` for
constant `c > 1`) has real cross-iteration dependencies, but they are
structured: every dependency index is provably smaller than the current index.
One way to execute this is a recursive fork-join scheduler with work-stealing,
where each task blocks until its dependency is ready. That needs a blocking
scheduler, a deadlock-freedom proof, and one task per index.

## Decision

Execute the DAG as a sequence of levels. Level `k` is the index range
`[c**k, c**(k+1))`. Because every dependency of an index in level `k` is
strictly below `c**k`, it lands in an earlier level. Levels run in ascending
order, each an ordinary flat parallel-for over chunks, with a barrier and commit
between levels. Reads in level `k` hit already-committed values from earlier
levels with zero interception.

No task ever waits on another task; the only synchronization is the level
barrier. Deadlock-freedom is therefore trivial ("finitely many levels,
processed in order") rather than an induction proof over blocking tasks. For a
million-element DAG with `c = 2`, this is about 20 dispatches instead of a
million task nodes.

The recursive fork-join scheduler was never built; this decision is what
shipped. Because each level is an ordinary batch, the DAG shape also runs on the
process backend without fine-grained cross-process waiting, which removed the
need for the `process_wait=` clause entirely.

## Consequences

- The scheduler is about two hundred lines of Python over the flat pool, not a
  work-stealing engine with a deadlock proof. This is the single largest
  complexity reduction in the design.
- The recognition rule's monotonicity constraint (dependency index provably at
  or below `i // c`) is now load-bearing in two ways: it proves correctness and
  it constructs the schedule. A change to the shape vocabulary that broke
  level-decomposability would require reintroducing a blocking scheduler, so the
  constraint stays in force.
- The `process_wait=` clause does not exist; a stale one in a config is rejected
  with a message pointing at this decision.

Spec: technical specification sections 5.5.3, 5.8.
