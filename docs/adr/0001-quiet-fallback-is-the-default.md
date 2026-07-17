# 0001. Quiet fallback is the default, not hard-fail

## Context

Lucen parallelizes what it can prove safe and runs everything else
sequentially. When a block cannot be parallelized, or when a parallel attempt
hits a runtime condition (a write conflict, a serialization failure, an
unpicklable argument), there are two possible responses: raise an error, or run
the block sequentially and record why.

A library that raises when it cannot parallelize would break programs that ran
correctly before Lucen was activated. That directly contradicts the
never-disruptive guarantee: adopting Lucen must never make a working program
stop working.

## Decision

The default behavior for every fallback is to run the block as sequential
Python and record a structured entry in the fallback report. Nothing is printed
to stderr, and nothing is raised. The result is always correct, because
sequential execution is always correct.

An opt-in `[errors].mode = "hard"` turns fallbacks into raised errors, and
per-block `strict=true` does the same for one block, for users who want CI to
fail when a hot loop silently stops parallelizing. These are opt-in precisely
because the safe default is to keep running.

The one exception is `ClauseValueError`: a malformed pragma clause is a
programming error in the marked file, raised unconditionally while the scanner
is parsing, independent of `[errors].mode`. This is not a runtime fallback; it
is a syntax error in the Lucen-specific surface, and failing loud is correct.

## Consequences

- Adopting Lucen is reversible and low-risk: the worst case is the program
  you already had, running sequentially.
- A block that silently stops parallelizing (for example after a refactor that
  introduces a dependency) is invisible unless the user reads the fallback
  report or opts into `strict`/`hard`. The `lucen explain --strict
  --baseline` gate exists to catch this in review.
- Every fallback path must be a real sequential re-run, not a partial or
  best-effort result. This is what makes the default safe.

Spec: technical specification section 5.14, section 1.1.
