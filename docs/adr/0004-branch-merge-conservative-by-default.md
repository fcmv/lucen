# 0004. Branch-merge is conservative by default

## Context

When a name is written on some branches of an `if` and not others, and then
read or committed after the branches merge, the analyzer cannot always prove
that the writes across iterations are disjoint. The write on one branch might
target the same index as a write on another branch in a different iteration.

Proving branch-sensitive disjointness in general requires reasoning about which
branch each iteration takes, which is runtime information.

## Decision

By default, an ambiguous write-only branch-merge routes the block to sequential.
Correctness is never gambled on a branch-sensitivity analysis that the analyzer
cannot complete statically.

The experimental `branch_sensitive_deps` flag opts into a more permissive
analysis: a write-only branch-merge conflict runs in parallel under the tier-C
runtime write-set audit, which re-runs the block sequentially if a real
cross-branch overlap is detected at join. The runtime audit is the safety net
that makes the optimistic path sound.

Writes to the same provably-distinct index across branches (for example
`ys[i]` on every branch) are already safe and do not need the flag; this
decision is only about the ambiguous case.

## Consequences

- The default is always correct: an unprovable branch-merge runs sequentially.
- The optimistic path is opt-in and still protected by the runtime audit, so
  even under the flag a real conflict produces the sequential result, not a
  wrong one.
- A class of blocks that could parallelize runs sequentially by default. This
  is the deliberate trade: correctness over coverage, with an opt-in for users
  who want the coverage and accept the audit cost.

Spec: technical specification section 5.3.3.
