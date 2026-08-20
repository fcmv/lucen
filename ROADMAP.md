# Roadmap

Planned work, paired with [LIMITATIONS.md](LIMITATIONS.md); most items close a
specific limitation and link back to it. Horizons are relative priority, not
dated commitments. Every item below is accepted and not yet started; anything
considered and declined is under [Not planned](#not-planned).

None of these changes the guarantee that a parallel run is bit-identical to the
same file run as plain sequential Python. An item found to require relaxing it
is dropped rather than shipped, which is why some optimizations appear under
[Not planned](#not-planned).

A performance item lands with a benchmark showing the win and a cross-backend
proof that the output is unchanged. A routing change lands only if
`tests/benchmarks/routing_check.py` still shows the gate selecting the fastest
backend on every workload.

- [Near term](#near-term)
- [Mid term](#mid-term)
- [Long term](#long-term)
- [Not planned](#not-planned)

---

## Near term

Self-contained items that close a known performance gap without new subsystems.

### N1. Cost model support for typed buffers

Closes [LIMITATIONS 3.1](LIMITATIONS.md#31-typed_buffers-is-not-in-the-cost-model).

Give the profitability gate a typed-transfer cost term so it selects the typed
process path automatically whenever that path is fastest. The `typed_buffers`
flag ships typed result slabs back from process workers, roughly an order of
magnitude cheaper than a list of the same floats, but the gate does not model
that, so a dense array-output map routes sequential and reaching the win today
needs an explicit `backend=process` plus the flag.

**Done when** the flag becomes a default the gate reaches for on its own, with
`routing_check.py` still green.

### N2. Reduction twin-probe

Closes [LIMITATIONS 3.2](LIMITATIONS.md#32-light-reductions-carry-a-small-probe-overhead).

Bring light reductions to the near-zero probe overhead pure maps already have.
Maps are probed on their sequential twin, which writes output in place, so the
probe chunk needs no private slab and no commit copy; a reduction's twin is
functional, so a light reduction over a large input pays a small overhead. The
fold must stay bit-identical, so the probe cannot change the order in which
partial contributions are combined.

**Done when** a reduction probe measures per-iteration cost without allocating a
throwaway slab, and light reductions match pure maps on probe overhead.

---

## Mid term

Items that add a capability or a build target and carry more design surface.

### M1. Free-threaded wavefront

Closes [LIMITATIONS 3.3](LIMITATIONS.md#33-the-recognized-dag-wavefront-runs-sequentially-by-default).

Let the gate select the level-synchronous wavefront (spec 5.8) automatically on
a free-threaded build when it is profitable. The recognized-DAG shape runs
sequentially by default because one pickled dispatch per level loses on a GIL
build; on a free-threaded build the level barriers are cheap and shared reads
hit already-committed values with no serialization.

**Done when** the gate selects the wavefront on a free-threaded build once
per-level width and per-node cost clear break-even, validated bit-identical
against sequential across the full recognized-shape vocabulary.

### M2. Free-threaded native core

Closes [LIMITATIONS 3.4](LIMITATIONS.md#34-no-native-core-on-free-threaded-builds).

Ship the native accelerator on free-threaded builds instead of falling back to
pure Python there. The core ships as one `abi3` wheel; free-threaded CPython has
no stable ABI, so that binary cannot load. Requires all of:

1. A separate non-`abi3` `cp3xt`-tagged wheel per free-threaded version. The
   PyO3 binding is already free-threaded-capable; the `abi3` wheel simply cannot
   be the vehicle there.
2. An explicit GIL-free module declaration. Without it CPython silently
   re-enables the GIL process-wide on import, destroying the property a user
   chose a free-threaded interpreter for. This is why the pure-Python fallback,
   which preserves free-threading, is the safe default today.
3. A thread-safety audit of the native entry points, which on a free-threaded
   build receive genuinely concurrent callers with no GIL serializing them.

This sits behind higher-value work: the native core accelerates two
orchestration primitives, and the free-threaded build is already competitive on
the fallback. It is recorded so the trade-off is visible rather than assumed.

### M3. SharedMemory result transfer

Return buffer-typed slabs from process workers through shared memory rather than
pickling, making the commit a memory map instead of a copy. This generalizes the
typed-buffer win (N1) to the return path. Shared segments must be released
cleanly on every exit path, including a mid-block error and a pool recycle,
verified by dedicated lifecycle tests.

### M4. Typed slabs beyond dense maps

Extend the typed-slab fast path from dense maps (straight-line bodies that write
every index) to shapes with proven-disjoint sparse writes, where a typed slab
plus a written-index record can still beat a Python list. The density proof that
guards the current path must be generalized without ever leaving a slot
undefined.

---

## Long term

### L1. Native loop-body compilation

Closes [LIMITATIONS 3.5](LIMITATIONS.md#35-the-loop-body-itself-is-never-compiled).

Compile the marked loop body, for a provably-typed numeric subset, to a native
kernel behind the same prove-or-fallback gate that governs everything else.
Today every iteration runs interpreted bytecode, so the speedup is bounded by
core count and the interpreter. Compiling the body turns the workloads the gate
currently keeps sequential into wins, because the interpreter and transfer costs
disappear rather than being amortized.

**Scope.** Prove the body is arithmetic and `math`-style calls over
uniformly-typed containers, emit a kernel that runs across shared-memory threads
with no pickling and no per-element interpreter dispatch, and fall back to
today's path for anything outside the subset. Bodies that call arbitrary user
Python are out of scope; compiling them means compiling the whole call graph,
which is a general JIT and a separate project.

**Constraints.** Two correctness properties are non-negotiable:

- **Python integers are unbounded.** A native `int64` kernel would wrap where
  Python promotes to a bignum, so bit-identical results demand checked
  arithmetic that bails to the interpreted path on overflow, or full bignum
  support in the kernel. Floats are easier: the same IEEE operations in the same
  per-element order reproduce Python's bits exactly, including the ordered fold.
- **Exception semantics must be preserved.** An error at a given iteration must
  leave the exact sequential-prefix state and raise the same exception type, so
  a kernel must detect the condition and bail to the sequential twin rather than
  producing a native trap.

---

## Not planned

Considered and declined, recorded so a contributor proposing one finds the
decision instead of re-opening it.

**Unmarked parallelization.** Lucen only touches marked blocks. Parallelizing
unmarked loops would mean guessing at intent and safety across a whole codebase,
which is incompatible with the never-wrong guarantee.

**Automatic verification of arbitrary callables.** General interprocedural
verification, including C extensions and fully dynamic dispatch, is undecidable
in the limit. The trust contract (LIMITATIONS 1) is the deliberate boundary.

**A blocking or work-stealing scheduler.** The recognized shapes are
level-decomposable, which makes a blocking scheduler unnecessary and keeps the
deadlock-freedom argument trivial: no task ever waits on another task. It would
be reconsidered only if a future recognized shape were proven not
level-decomposable, which would mean breaking the monotonicity constraint on the
dependency vocabulary. That constraint stays in force.

**Vectorization advice in `explain`.** Advising that a block should be a NumPy
expression is the job of a linter for numeric code, not of a parallelizer. Lucen
parallelizes the loop as written and declines when parallelism cannot win; it
does not rewrite the algorithm.
