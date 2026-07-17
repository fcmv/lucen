# Architecture Decision Records

One file per non-obvious decision. Filing them here means a future contributor
proposing to "simplify" one of these finds the reasoning immediately, instead
of re-introducing a bug the design already fixed once. Each record links back
to the [technical specification](../spec/lucen_technical_spec.md) section
that governs the decision.

Format: Context, Decision, Consequences, one page maximum.

## Filed

- [0001](0001-quiet-fallback-is-the-default.md) quiet fallback is the default, not hard-fail
- [0003](0003-trust-is-restricted-and-only-proof-downgrades.md) trust is restricted by default and only positive proof downgrades
- [0004](0004-branch-merge-conservative-by-default.md) branch-merge is conservative by default
- [0005](0005-nested-region-guard-is-silent.md) the nested-region guard is silent, not a hard error
- [0006](0006-reduction-order-is-sequential-equivalent.md) reduction order defaults to sequential-equivalent
- [0007](0007-chunk-is-the-unit-of-dispatch.md) the chunk is the unit of dispatch
- [0008](0008-privatize-and-commit.md) privatize-and-commit replaces versioned cells
- [0009](0009-wavefront-replaces-fork-join.md) level-synchronous wavefront replaces fork-join
- [0010](0010-profitability-gate-and-probe.md) profitability gate with a sequential-prefix probe
- [0011](0011-thread-timeouts-are-cooperative.md) thread timeouts are cooperative
- [0013](0013-interpreter-independent-routing.md) backend routing is interpreter-independent
- [0014](0014-native-orchestration-by-reference.md) native orchestration folds by reference

## Superseded

- 0002 recursive fork-join scheduler. Superseded by
  [0009](0009-wavefront-replaces-fork-join.md); the fork-join scheduler was
  never built.
