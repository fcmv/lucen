# Formal specifications

Lucen guarantees that a parallel run is bit-identical to plain sequential
Python. The two mechanisms that guarantee hangs on are model-checked here, so
the correctness argument is mechanical rather than only prose.

Each protocol is specified twice:

- a **TLA+ specification** (`*.tla`, checked by the TLC model checker), the
  industry-standard formal artifact; and
- an **executable bounded model checker** in the test suite
  ([tests/formal/test_model_check.py](https://github.com/fcmv/lucen/blob/main/tests/formal/test_model_check.py)),
  which exhaustively explores the same state space in Python and runs in CI
  with no external tooling.

Exhaustive exploration of a bounded state space is a proof for that bound. The
two encodings check the same properties by different means, which is the point:
the runnable one gives continuous assurance, the TLA+ one gives the reviewable
formal statement.

## What is proven

### PrivatizeCommit ([`PrivatizeCommit.tla`](PrivatizeCommit.tla), [ADR 0008](../adr/0008-privatize-and-commit.md))

The iteration space is partitioned into contiguous chunks. Each chunk executes
concurrently, in any order, writing only into its own private slab. At join the
slabs commit into the shared array in chunk order.

Property checked: **SequentialEquivalence.** For every interleaving of chunk
execution, once committed the shared array equals the sequential result.
Because each chunk writes a private slab, the execution order cannot affect the
outcome; the ordered commit is what makes the result sequential-equivalent. The
executable checker additionally shows the write-set audit detects a cross-chunk
write conflict, which is what forces the sequential fallback rather than a wrong
result.

### Wavefront ([`Wavefront.tla`](Wavefront.tla), [ADR 0009](../adr/0009-wavefront-replaces-fork-join.md))

A recognized-DAG block with dependency `i div C` is executed level by level:
level `k` is the set of indices at dependency-depth `k`, levels run in order
with a barrier between them, and within a level indices run concurrently.

Properties checked: **DependencySafety** (a safety invariant) and
**Termination** (a liveness property). Every committed index read its
dependency only after that dependency was committed, because a dependency is
always in a strictly earlier, already-committed level; and the schedule always
advances to completion, which is the deadlock-freedom argument for the
scheduler. No task ever waits on another task.

## Running the model checker

The executable checker runs as part of the normal suite:

```bash
pytest tests/formal
```

The TLA+ specifications need Java and the TLA+ tools (`tla2tools.jar`, from the
[tlaplus releases](https://github.com/tlaplus/tlaplus/releases)):

```bash
java -cp tla2tools.jar tlc2.TLC -deadlock -config PrivatizeCommit.cfg PrivatizeCommit.tla
java -cp tla2tools.jar tlc2.TLC -deadlock -config Wavefront.cfg Wavefront.tla
```

Both report `Model checking completed. No error has been found`. The `-deadlock`
flag disables TLC's terminal-state check, since a finished computation is a
terminal state by design, not a deadlock.

The configured bounds (a handful of iterations, two or three chunks, small
divisors) are chosen so the state space is exhaustively explorable in under a
second. The protocols are parametric, and the argument generalizes; the bounded
check is what turns the argument into a mechanically verified fact.
