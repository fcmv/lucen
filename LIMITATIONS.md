# Limitations

This document is the honest inventory of what Lucen does not do, where it
leaves performance on the table, and the exact boundary of its correctness
guarantee. It is maintained to the same standard as the code: every entry
states what the limitation is, why it exists, what to do about it, and where
it is headed. If you hit something that behaves worse than this document
promises, that is a bug; please file it.

One thing is deliberately absent from every list below: an incorrect result.
Lucen has a single guarantee with no tier and no opt-out, which is that a
parallel run is bit-identical to the same file executed as plain sequential
Python. Everything in this document is about speed, scope, observable
semantics that differ without being wrong, or the narrow boundary at which the
correctness guarantee becomes a documented trust contract. None of it is a
case where Lucen silently returns a wrong answer inside its own contract.

The limitations are grouped into four categories:

1. [The correctness boundary](#1-the-correctness-boundary): the trust
   contract, where divergence is possible and what it requires.
2. [Inherent parallel semantics](#2-inherent-parallel-semantics): behavior
   that differs from sequential without being wrong.
3. [Performance gaps](#3-performance-gaps): where Lucen is correct but
   slower than it could be.
4. [Scope limits](#4-scope-limits): what Lucen does not attempt.

Planned work against these items is tracked in [ROADMAP.md](ROADMAP.md).
Measured numbers behind the performance claims are in
[BENCHMARK.md](BENCHMARK.md). The full semantics are in the
[technical specification](docs/spec/lucen_technical_spec.md).

---

## 1. The correctness boundary

Lucen guarantees a marked file behaves identically to the same file with
the pragmas treated as comments, provided your objects and helpers tell the
truth about themselves. Two adversarial red-team campaigns (over 130 scenarios
across a black-box third-party posture and a competitor posture) found that
every silent divergence reduces to one of the three items below, each of which
is code the analyzer cannot see inside. These are the trust contract. They are
enforced where enforcement is possible and documented where it is not.

### 1.1 Helper purity beyond readable source

**What.** A helper callable that carries hidden mutable state (mutates a module
global, advances a closure cell, consumes `random` state, performs I/O) is
correct under sequential execution but can diverge per worker on the process
backend, where each worker holds its own copy of that state.

**What Lucen does about it.** The purity proof (spec 5.4) statically reads
the helper's source where it can. A helper it can *prove* mutates hidden state
makes its block run sequentially, with a report naming the helper and the
reason. This closes the case for any helper whose source is importable and
analyzable.

**Where it remains.** A helper whose body the analyzer cannot read keeps the
documented args-as-reads trust. That is:

- C extension functions (no Python source to analyze).
- Callables reached through fully dynamic dispatch that the analyzer cannot
  resolve to a definition.

A stateful helper hiding behind one of these boundaries diverges silently on
the process backend. This is the sharpest edge of the contract.

**What to do.** Keep helpers called from a marked block pure with respect to
hidden state. If a helper is safe but the analyzer cannot see that (for example
a C-level function you know is pure), assert it with `# LUCEN TRUST` on its
`def`, `trust=callables` on the block, or a `[trust] callables` entry in
`lucen.toml`. If a helper is genuinely stateful and must run in loop order,
do not parallelize the block; the purity proof will already route it
sequentially when it can prove the impurity.

**Severity.** Medium. Bounded to unreadable stateful helpers; the readable case
is proven and downgraded automatically.

### 1.2 Faithful serialization

**What.** The process backend ships argument bundles by pickle. An object whose
serialization does not preserve value (a `__reduce__` or `__getstate__` that
shifts state on every round trip) would arrive at the worker changed.

**What Lucen does about it.** The preflight gate (spec 5.13) verifies that
the first chunk's argument bundle reaches a byte fixed point after one pickle
round trip. An accumulating serializer never converges, and the block falls
back sequentially and loudly. This catches the demonstrated silent-corruption
exploit (an object that adds a constant on every round trip).

**Where it remains.** A serializer that oscillates with a period greater than
one (returns to a byte-identical state after N round trips for N greater than
one) can pass the convergence check while still not being value-preserving in
transit. This is a pathological and, in practice, buggy serializer; the
convergence check catches the common accumulating form, not every conceivable
adversarial period.

**What to do.** Objects passed into a marked block must pickle faithfully, which
is already an expectation of any code that uses `multiprocessing`. `trust=pickle`
waives the convergence check when you have verified the object yourself.

**Severity.** Low. Requires a deliberately or unusually broken serializer;
the common accumulating form is caught.

### 1.3 Two explicit assertions

**What.** `depend=none` asserts your indexed writes are disjoint;
`skip_runtime_check=true` additionally disables the runtime write-set audit
that would catch a false `depend=none`. With both asserted on a block whose
writes actually overlap, Lucen produces a wrong result, exactly as the
clauses say they will.

**Why it is here and not a bug.** This is the documented escape hatch for an
expert who has proven disjointness the analyzer cannot. Red-team testing
confirmed the important property: one assertion is never enough. `depend=none`
alone on a real dependency is still caught by the tier-C audit and re-runs
sequentially. It takes two explicit assertions, each a deliberate waiver, to
reach a silent wrong result.

**What to do.** Do not combine `depend=none` with `skip_runtime_check=true`
unless you have independently proven the writes are disjoint. If you are unsure,
drop `skip_runtime_check=true` and let the audit protect you at a small cost.

**Severity.** Low by design. Reachable only by two deliberate expert waivers.

---

## 2. Inherent parallel semantics

These behaviors differ from sequential execution. They are not wrong (the
committed result is bit-identical), but they are observably different if your
code looks at them, and they cannot be fixed without giving up parallelism.

### 2.1 Side-effect order is not sequential

**What.** Inside a block that parallelizes while still performing side effects
(for example under an explicit trust assertion), the *count* of every side
effect is exact, but the *order* is not sequential. Two hundred `print`
statements from a parallel body all appear exactly once each, interleaved by
chunk rather than in loop order.

**Note.** Where a side effect is statically detectable, the purity proof
(spec 5.4) routes the block to sequential precisely to preserve order, so this
surfaces mainly under an explicit trust assertion that overrides that proof.

**What to do.** If a block's side effects must be ordered (streamed log lines,
appended output), do not force it parallel. Consumers of unordered side effects
(counters, independent writes) are unaffected.

**Severity.** Low. Count fidelity is perfect; order fidelity is not promised for
side-effecting parallel blocks.

### 2.2 Executor-observing code sees workers

**What.** Code that observes the execution environment (`os.getpid()`, thread
identity, thread-local state) sees the worker that ran the iteration, not the
single main process or thread it would see sequentially.

**What to do.** This is intrinsic to running on more than one worker. If a loop
body's result depends on the process or thread it runs in, it is not a
parallelizable body.

**Severity.** Low. Observing the executor is outside the value contract; the
computed result is unaffected.

### 2.3 Spawn platforms need the `__main__` guard

**What.** On spawn platforms (Windows, and macOS by default), the process
backend re-imports the entry module in every worker. An entry script that does
work at import time without an `if __name__ == "__main__":` guard would re-run
that work in each worker.

**What Lucen does about it.** The spawn-safety scan (spec 5.10) detects an
unguarded entry script in the parent, before any worker spawns, and runs the
block sequentially with an actionable message instead of the child-side error
flood that `multiprocessing` would otherwise produce. The result stays correct;
the cost is that the block does not parallelize.

**What to do.** Put your program's top-level work behind
`if __name__ == "__main__":`, or drive it from a small entry file that does so.
This is the standard `multiprocessing` requirement.

**Severity.** Low. Detected and handled; the fix is a one-line guard.

---

## 3. Performance gaps

Here Lucen is correct but slower than it could be. Each of these is a case
where the profitability gate or the routing is conservative, or where a faster
path exists but is not yet the default. None of them affect output.

### 3.1 `typed_buffers` is not in the cost model

**What.** The experimental `typed_buffers` flag ships typed result slabs
(array or bytearray) back from process workers, roughly an order of magnitude
cheaper than a list of the same floats. The profitability gate does not yet
account for this cheaper transfer, so it routes array-output maps to sequential
even where the typed process path would win.

**What to do.** For a dense array-output map, enable the flag and force the
backend: `activate(experimental=["typed_buffers"])` plus a
`# LUCEN START backend=process` on the block. This is opt-in and does not
change correctness.

**Roadmap.** Teach the cost model the typed transfer cost and revalidate
routing. See [ROADMAP.md](ROADMAP.md).

### 3.2 Light reductions carry a small probe overhead

**What.** A trivial reduction over a large input (summing a million elements)
carries roughly a five to ten percent probe overhead relative to sequential,
because reductions cannot yet use the twin-probe fast path that pure maps use.
A reduction's sequential twin is functional (it returns the accumulator) rather
than in-place, so probing it still needs a chunk-function slab.

**What to do.** Nothing is required; the block is still correct and the overhead
is small. If a specific light reduction is hot, `calibrate=false` forces the
parallel path.

**Roadmap.** A reduction twin-probe to close this gap. See
[ROADMAP.md](ROADMAP.md).

### 3.3 The recognized-DAG wavefront runs sequentially by default

**What.** A recognized-DAG block (`results[i] = combine(results[i // 2], ...)`)
has a level-synchronous parallel form (spec 5.8), but it runs sequentially by
default. Its parallel form pays off only on a free-threaded build under an
explicit `backend=thread`; on a GIL build one pickled dispatch per level loses
badly to sequential.

**What to do.** On a free-threaded build, add `backend=thread` to a
recognized-DAG block that is heavy enough to benefit. On a GIL build, sequential
is the correct default for this shape.

**Roadmap.** Free-threaded wavefront enablement. See [ROADMAP.md](ROADMAP.md).

### 3.4 No native core on free-threaded builds

**What.** The native accelerator ships as a single `abi3` wheel that loads on
GIL builds 3.9 through 3.14. The stable ABI and the free-threaded ABI are
mutually exclusive, so the `abi3` binary cannot load on a free-threaded
interpreter. On a free-threaded build, `pip install lucen` selects the
published `py3-none-any` pure-Python wheel instead, and Lucen runs its
pure-Python fallback. The install path is not affected; only the native
acceleration is absent.

**Impact.** Small. The native core accelerates two orchestration primitives (the
write-set audit and the reduction fold), not the loop body or the dispatch. The
pure-Python fallback is complete, fully tested, and on the benchmark matrix the
free-threaded build posts competitive numbers without it.

**Roadmap.** A separate non-`abi3` free-threaded native build, with the full set
of requirements (a free-threaded-capable PyO3, an explicit GIL-free module
declaration, and a concurrency audit of the native entry points). See
[ROADMAP.md](ROADMAP.md).

### 3.5 The loop body itself is never compiled

**What.** Lucen parallelizes the loop; it does not compile the loop body.
Every iteration runs the same interpreted Python bytecode it would run
sequentially, on whichever worker executes it. For a body that is interpreter
work (arithmetic over Python objects), the speedup is bounded by the core count
and the interpreter, not by native code generation.

**Roadmap.** Native compilation of a provably-typed numeric subset of loop
bodies is the flagship roadmap item. It is compiler-scale work and is not a
current feature. See [ROADMAP.md](ROADMAP.md).

---

## 4. Scope limits

What Lucen does not attempt. These are boundaries of the design, not defects.

### 4.1 One block per pragma pair, one loop per block

A `# LUCEN START` / `# LUCEN END` pair marks exactly one `for` loop. The
body may contain arbitrarily nested control flow, but the marked construct
itself is a single `for` loop over a sized iterable. A marked `while` has no
iteration space to chunk and runs as unmodified Python. `async` loop bodies are
out of scope.

### 4.2 Sized iterables only

Chunked dispatch requires a known length. A marked loop over an unsized iterable
(a bare generator) takes the `UnsupportedIterableError` fallback and runs
sequentially. Materialize the iterable to a list first if you want it
parallelized.

### 4.3 `break` and `return` are sequential outside an experimental flag

A block containing `break` or `return` runs sequentially unless the
experimental early-exit scheduler is enabled
(`activate(experimental=["early_exit"])`), which reproduces sequential
first-break semantics speculatively. Without the flag, correctness is preserved
by running the block sequentially.

### 4.4 Not a substitute for vectorized kernels

Lucen accelerates Python-level loop bodies of meaningful size. A loop that
should be a single NumPy expression will be faster as that expression than as a
parallelized Python loop, and Lucen does not claim otherwise. The
profitability gate is the mechanical expression of this honesty: when parallel
dispatch cannot win, Lucen stays sequential and says so.

### 4.5 No guaranteed speedup

The profitability gate is a bounded-cost estimator, not a promise. It can
mispredict; when it does, the cost is bounded at roughly one chunk of
suboptimal scheduling per calibration cycle, it is visible in
`lucen profile`, and it never changes output. A block that is genuinely too
small to benefit runs sequentially by design.

---

*The correctness guarantee is not on any of these lists, and that is the point.*
