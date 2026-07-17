# Roadmap

This roadmap lists Lucen's planned work: the goal of each item, why it matters,
its status, and how it will be judged complete. It is paired with
[LIMITATIONS.md](LIMITATIONS.md); most items close a specific limitation and
link back to it.

Horizons express relative priority, not dated commitments. Every item improves
speed, scope, or ergonomics; none changes the correctness guarantee that a
parallel run is bit-identical to the same file run as plain sequential Python.
An item found to require relaxing that guarantee is dropped rather than shipped,
which is why some optimizations appear under [Not planned](#not-planned).

## How items ship

- A performance item lands with a benchmark showing the win and a cross-backend
  equivalence proof that the output is unchanged.
- A routing change lands only if `tests/benchmarks/routing_check.py` still shows
  the gate selecting the fastest backend on every workload.

## Status legend

- **Planned:** accepted; work not yet started.
- **In progress:** actively under development.
- **Declined:** considered and not pursued (see [Not planned](#not-planned)).

- [Near term](#near-term)
- [Mid term](#mid-term)
- [Long term](#long-term)
- [Not planned](#not-planned)

---

## Near term

Self-contained items that close a known performance gap without new subsystems.

### N1. Cost model support for typed buffers

**Status:** Planned. **Closes:** [LIMITATIONS 3.1](LIMITATIONS.md#31-typed_buffers-is-not-in-the-cost-model).

**Goal.** Give the profitability gate a typed-transfer cost term so it selects
the typed process path automatically whenever that path is the fastest.

**Why.** The `typed_buffers` flag ships typed result slabs (array, bytearray)
back from process workers, roughly an order of magnitude cheaper than a list of
the same floats. The gate does not yet model this cheaper transfer, so a dense
array-output map is routed to sequential even when the typed path would win, and
reaching that win today requires an explicit `backend=process` plus the flag.

**Done when.** The flag becomes a default the gate reaches for on its own, with
`routing_check.py` still green.

### N2. Reduction twin-probe

**Status:** Planned. **Closes:** [LIMITATIONS 3.2](LIMITATIONS.md#32-light-reductions-carry-a-small-probe-overhead).

**Goal.** Bring light reductions to the same near-zero probe overhead that pure
maps already have.

**Why.** Pure maps are probed on their sequential twin, which writes output in
place, so the probe chunk needs no private slab and no commit copy. A
reduction's twin is functional (it returns the accumulator) rather than
in-place, so a light reduction over a large input carries a small probe
overhead relative to sequential.

**Constraint.** The fold must remain bit-identical, so the probe cannot change
the order in which partial contributions are combined.

**Done when.** A reduction probe measures per-iteration cost without allocating
a throwaway slab, and light reductions match pure maps on probe overhead.

---

## Mid term

Items that add a capability or a build target and carry more design surface.

### M1. Free-threaded wavefront

**Status:** Planned. **Closes:** [LIMITATIONS 3.3](LIMITATIONS.md#33-the-recognized-dag-wavefront-runs-sequentially-by-default).

**Goal.** Let the gate select the level-synchronous wavefront (spec 5.8)
automatically on a free-threaded build when it is profitable.

**Why.** The recognized-DAG shape runs sequentially by default because, on a GIL
build, one pickled dispatch per level loses to sequential. On a free-threaded
build the level barriers are cheap and shared reads hit already-committed values
with no serialization, so the wavefront should be a default there rather than
requiring an explicit `backend=thread`.

**Done when.** The gate selects the wavefront on a free-threaded build once the
per-level width and per-node cost clear break-even, validated bit-identical
against sequential across the full recognized-shape vocabulary.

### M2. Free-threaded native core

**Status:** Planned. **Closes:** [LIMITATIONS 3.4](LIMITATIONS.md#34-no-native-core-on-free-threaded-builds).

**Goal.** Ship the native accelerator on free-threaded builds instead of
falling back to pure Python there.

**Why.** The native core ships as one `abi3` wheel that loads on every GIL build
from 3.9 through 3.14. Free-threaded CPython does not support the stable ABI, so
the `abi3` binary cannot load there and Lucen runs its pure-Python fallback.

**Requires** all of:

1. A separate non-`abi3` `cp3xt`-tagged wheel per free-threaded version. The
   PyO3 binding is already free-threaded-capable; the `abi3` wheel simply cannot
   be the vehicle there, so a distinct wheel is needed.
2. An explicit GIL-free module declaration. Without it, CPython silently
   re-enables the GIL process-wide when the extension is imported, which would
   destroy the property a user chose a free-threaded interpreter for. This is
   why the pure-Python fallback, which preserves free-threading, is the safe
   default today.
3. A thread-safety audit of the native entry points, which on a free-threaded
   build receive genuinely concurrent callers with no GIL serializing them.

**Sequencing.** This item sits behind higher-value work: the native core
accelerates two orchestration primitives, and the free-threaded build is
already competitive on the pure-Python fallback. It is recorded here so the
trade-off is visible rather than assumed.

### M3. SharedMemory result transfer

**Status:** Planned.

**Goal.** Return buffer-typed slabs from process workers through shared memory
rather than pickling, making the commit a memory map instead of a copy.

**Why.** This generalizes the typed-buffer transfer win (N1) to the return path
and pairs naturally with the cost-model work there.

**Constraint.** Shared segments must be released cleanly on every exit path,
including a mid-block error and a pool recycle, verified by dedicated lifecycle
tests.

### M4. Typed slabs beyond dense maps

**Status:** Planned.

**Goal.** Extend the typed-slab fast path from dense maps (straight-line bodies
that write every index) to shapes with proven-disjoint sparse writes, where a
typed slab plus a written-index record can still beat a Python list.

**Constraint.** The density proof that guards the current path must be
generalized without ever leaving a slot undefined.

---

## Long term

The single largest lever, and the most work.

### L1. Native loop-body compilation

**Status:** Planned. **Closes:** [LIMITATIONS 3.5](LIMITATIONS.md#35-the-loop-body-itself-is-never-compiled).

**Goal.** Compile the marked loop body, for a provably-typed numeric subset, to
a native kernel behind the same prove-or-fallback gate that governs everything
else.

**Why.** Today Lucen parallelizes the loop but never compiles the body: every
iteration runs interpreted bytecode on whichever worker executes it. For a body
that is interpreter work over Python objects, the speedup is bounded by the core
count and the interpreter, not by native code. Compiling the body turns the
workloads the gate currently keeps sequential (because dispatch and transfer
outweigh a trivial body) into large wins, because the interpreter and transfer
costs disappear rather than being amortized.

**Scope.** Prove the body is arithmetic and `math`-style calls over
uniformly-typed containers, emit a native kernel that runs across shared-memory
threads with no pickling and no per-element interpreter dispatch, and fall back
to today's path for anything outside the subset. Bodies that call arbitrary
user Python functions are out of scope; compiling them would mean compiling the
whole call graph, which is a general just-in-time compiler and a separate
project.

**Constraints.** Two correctness properties are non-negotiable:

- **Python integers are unbounded.** A native `int64` kernel would wrap where
  Python promotes to a bignum. Bit-identical results demand checked arithmetic
  that falls back to the interpreted path on overflow, or full bignum support in
  the kernel. Floats are the easier case: the same IEEE operations in the same
  per-element order reproduce Python's bits exactly, including the ordered
  reduction fold.
- **Exception semantics must be preserved.** An error at a given iteration must
  leave the exact sequential-prefix state and raise the same exception type, so
  a kernel must detect the condition and bail to the sequential twin rather than
  producing a native trap.

---

## Not planned

Items considered and declined, recorded so a contributor proposing one finds the
decision instead of re-opening it.

### Unmarked parallelization

**Declined.** Lucen only touches marked blocks. Parallelizing unmarked loops
would mean guessing at intent and safety across a whole codebase, which is
incompatible with the never-wrong guarantee and the Comment Invariant.

### Automatic verification of arbitrary callables

**Declined.** The purity proof reads source where it can and trusts the user for
the rest. General interprocedural verification of arbitrary callables, including
C extensions and fully dynamic dispatch, is undecidable in the limit. The trust
contract (LIMITATIONS 1) is the deliberate boundary.

### A blocking or work-stealing scheduler

**Declined.** The recognized-DAG wavefront exists because the recognized shapes
are level-decomposable, which makes a blocking scheduler unnecessary and keeps
the deadlock-freedom argument trivial (no task ever waits on another task). It
would be reconsidered only if a future recognized shape were proven not
level-decomposable, which would mean breaking the monotonicity constraint on the
dependency vocabulary. That constraint stays in force.

### Vectorization advice in `explain`

**Declined.** Advising that a block should be a NumPy expression is the job of a
linter for numeric code, not of a parallelizer. Lucen parallelizes the loop as
written, and its profitability gate declines when parallelism cannot win; it
does not rewrite the algorithm.
