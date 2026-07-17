# 0005. The nested-region guard is silent, not a hard error

## Context

A marked loop body can call a function that itself contains a marked loop. If
the outer loop is already dispatching across a pool, the inner loop would try to
dispatch across the same or a nested pool, oversubscribing workers or, in the
worst case, deadlocking.

The library needs to prevent an inner marked region from dispatching while an
outer one is active.

## Decision

A dispatch-active flag is carried per execution context. When a marked block is
reached while that flag is set, the inner region runs sequentially, silently.
It is not tiered by `[errors].mode`, and it does not raise even under `hard`.

The reasoning: nesting is a structural property of how the user's functions
compose, not a mistake in any single marked block. The outer block is running
in parallel; the inner one running sequentially inside it is correct and is
usually what the user would want anyway (the outer parallelism already fills the
cores). Raising here would break correct programs for a non-error.

The `nested=` clause governs reporting and a reserved opt-in only; it does not
change the default silent-sequential behavior.

## Consequences

- Composing parallelized functions never deadlocks and never raises for the
  nesting itself.
- The inner region's sequential execution is correct, and the outer
  parallelism already uses the machine.
- A user who wants to know when nesting suppressed inner parallelism uses the
  reporting clause; the default stays quiet because nesting is expected in real
  code.

Spec: technical specification section 5.11.
