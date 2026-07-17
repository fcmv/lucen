# 0003. Trust is restricted by default and only positive proof downgrades

## Context

A marked loop body usually calls helper functions. Lucen must decide whether
a helper is safe to run in parallel, meaning it does not mutate shared hidden
state that sequential execution would have serialized. It cannot always know:
the helper may be a C extension, or dynamically dispatched, or otherwise
unreadable.

Two failure directions exist. If Lucen trusts too much, a stateful helper
diverges silently on the process backend (each worker gets its own copy of the
state). If Lucen trusts too little, it downgrades safe blocks to sequential
and the library stops being useful.

## Decision

Two rules, applied together:

1. **Arguments are trusted as reads, not the callable's whole behavior.** A
   called function's arguments are classified as reads of the containers they
   come from. This is the minimal assumption that lets ordinary pure helpers
   parallelize.

2. **Only positive proof of impurity downgrades a block.** The purity analysis
   reads a helper's source where it can. If it can *prove* the helper mutates a
   module global, a closure cell, `random` state, or performs I/O (transitively,
   up to a depth cap), the block runs sequentially with a report naming the
   helper. If it cannot read the helper, or cannot prove impurity, the block
   keeps its parallel routing. Absence of proof is never treated as proof of
   impurity.

The user can override in either direction. `# LUCEN TRUST` on a helper's
`def`, `trust=callables` on a block, or `[trust] callables` in config asserts a
helper is safe when the analyzer cannot see that. There is no clause to force a
proven-impure helper parallel, because that would be asserting a falsehood the
analyzer has already disproved.

## Consequences

- Existing parallel routing never regresses when the purity proof is added: a
  block only loses its backend if impurity is proven, which by construction did
  not happen for any block that was correct before.
- The residual risk is precise and documented: a stateful helper the analyzer
  cannot read (C extension, dynamic dispatch) still diverges silently on the
  process backend. This is the trust boundary, recorded in LIMITATIONS.md, not
  a bug to be surprised by.
- The two-direction override keeps the naive path automatic and the expert path
  in control, with each override a single explicit assertion.

Spec: technical specification sections 5.3.4, 5.4.
