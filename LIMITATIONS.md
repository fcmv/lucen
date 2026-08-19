# Limitations

What Lucen does not do, where it leaves performance on the table, and the exact
boundary of its correctness guarantee. Everything here is about speed, scope, or
observable semantics that differ without being wrong; none of it is a case where
Lucen silently returns a wrong answer inside its own contract. If you hit
something that behaves worse than this document promises, that is a bug.

Planned work against these items is in [ROADMAP.md](ROADMAP.md), the measured
numbers are in [BENCHMARK.md](BENCHMARK.md), and the full semantics are in the
[technical specification](docs/spec/lucen_technical_spec.md).

1. [The correctness boundary](#1-the-correctness-boundary)
2. [Inherent parallel semantics](#2-inherent-parallel-semantics)
3. [Performance gaps](#3-performance-gaps)
4. [Scope limits](#4-scope-limits)

---

## 1. The correctness boundary

Lucen guarantees a marked file behaves identically to the same file with the
pragmas treated as comments, provided your objects and helpers tell the truth
about themselves. Two red-team campaigns (over 130 scenarios) found that every
silent divergence reduces to one of the three items below, each of them code the
analyzer cannot see inside. This is the trust contract.

### 1.1 Helper purity beyond readable source

A helper that carries hidden mutable state (mutates a module global, advances a
closure cell, consumes `random` state, performs I/O) is correct sequentially but
can diverge per worker on the process backend, where each worker holds its own
copy of that state.

The purity proof (spec 5.4) statically reads the helper's source where it can,
and a helper it *proves* stateful makes its block run sequentially with a report
naming it. That closes the case for any helper whose source is importable and
analyzable. What remains is helpers the analyzer cannot read: C extension
functions, and callables reached through dynamic dispatch it cannot resolve to a
definition. Those keep the documented args-as-reads trust, and a stateful one
hiding behind that boundary diverges silently. This is the sharpest edge of the
contract.

Keep helpers called from a marked block pure with respect to hidden state. If
one is safe but unreadable (a C-level function you know is pure), assert it with
`# LUCEN TRUST` on its `def`, `trust=callables` on the block, or a
`[trust] callables` entry in `lucen.toml`.

### 1.2 Faithful serialization

The process backend ships argument bundles by pickle, so an object whose
serialization does not preserve value would arrive at the worker changed. The
preflight gate (spec 5.13) verifies that the first chunk's bundle reaches a byte
fixed point after one round trip; an accumulating serializer never converges and
the block falls back sequentially and loudly.

A serializer that oscillates with a period greater than one can pass the
convergence check while still not being value-preserving in transit. That is a
pathological serializer, and the check catches the common accumulating form
rather than every conceivable period. Objects passed into a marked block must
pickle faithfully, which `multiprocessing` already expects; `trust=pickle`
waives the check when you have verified the object yourself.

### 1.3 Two explicit assertions

`depend=none` asserts your indexed writes are disjoint. `skip_runtime_check=true`
additionally disables the runtime write-set audit that would catch a false
`depend=none`. With both asserted on a block whose writes actually overlap,
Lucen produces a wrong result, exactly as the clauses say they will.

This is the documented escape hatch for an expert who has proven disjointness
the analyzer cannot, and red-team testing confirmed the property that matters:
one assertion is never enough. `depend=none` alone on a real dependency is still
caught by the tier-C audit and re-runs sequentially. Do not add
`skip_runtime_check=true` unless you have independently proven the writes are
disjoint.

---

## 2. Inherent parallel semantics

These behaviors differ from sequential execution. The committed result is still
bit-identical, but they are observably different if your code looks at them, and
they cannot be fixed without giving up parallelism.

### 2.1 Side-effect order is not sequential

Inside a block that parallelizes while still performing side effects, the
*count* of every side effect is exact but the *order* is not. Two hundred
`print` statements from a parallel body all appear exactly once each,
interleaved by chunk rather than in loop order.

Where a side effect is statically detectable the purity proof routes the block
sequentially precisely to preserve order, so this surfaces mainly under an
explicit trust assertion that overrides that proof. If a block's side effects
must be ordered, do not force it parallel.

### 2.2 Executor-observing code sees workers

Code that observes its execution environment (`os.getpid()`, thread identity,
thread-local state) sees the worker that ran the iteration rather than the
single process or thread it would see sequentially. This is intrinsic to running
on more than one worker: if a body's result depends on where it runs, it is not
a parallelizable body.

### 2.3 Spawn platforms need the `__main__` guard

On spawn platforms (Windows, and macOS by default) the process backend
re-imports the entry module in every worker, so an entry script that does work
at import time would re-run that work in each worker. The spawn-safety scan
(spec 5.10) detects an unguarded entry script in the parent, before any worker
spawns, and runs the block sequentially with an actionable message instead of
the child-side error flood `multiprocessing` would produce. The result stays
correct; the cost is that the block does not parallelize. Put top-level work
behind `if __name__ == "__main__":`, as `multiprocessing` already requires.

### 2.4 Destructors run in the worker that owns the object

An object created and released inside the loop body has its `__del__` run on the
worker. Under the process backend the object never exists in the parent at all,
so a destructor side effect (appending to a module list, flushing a handle) is
performed in the child and never observed by the parent.

The purity proof does not catch this: a destructor is not called anywhere in the
source it reads, it is attached to a type and fires when the interpreter
releases the object, so a body that allocates such an object reads as pure. Do
not rely on `__del__` for program-visible effects in a marked loop; release
timing is an implementation detail in plain Python too. Use an explicit
`close()`, a `with` block, or return the value and act on it after the loop.

### 2.5 Exception type can degrade across the process boundary

An exception raised on the process backend crosses a pickle boundary before it
reaches you. The traceback never survives that crossing, because pickle does not
carry `__traceback__`, and in one narrow case the type does not either.

Each worker-side exception is round-trip tested. One that pickles is returned as
itself, with type, message and arguments intact. One that does not is replaced
by a proxy carrying its module, qualname and message, which the parent
re-imports and rebuilds by calling the type with the message as its single
argument. An exception whose constructor needs more than that cannot be
reconstructed, and the parent raises `RuntimeError` carrying the original
qualname and message in its text. Such a block raises `RuntimeError` under the
process backend and its own type under sequential or `backend=thread`;
`on_error=collect` degrades the same way, through the same path.

Keep exceptions that can escape a marked block picklable. Where the type matters
and cannot be made picklable, pin the block with `backend=thread`, which raises
the original exception object with its traceback intact.

### 2.6 Process workers multiply the memory footprint

A process worker is a separate interpreter holding its own copy of whatever the
loop body reaches. On a spawn platform each worker re-imports the entry module
and the module graph behind the body, so peak memory is roughly the parent's
footprint plus one import graph per worker; on a fork platform the children
start as copy-on-write images, which is far cheaper. Either way the growth
arrives at the first process dispatch rather than accumulating, and the
multiplier is the size of your imports, not the size of your data.

The pool is created once per process and reused across every block, so a program
with twenty marked blocks pays for one set of workers, and a container read only
as `P[i]` ships as a slice rather than whole. Size the pool for your import
graph rather than your core count: `backend=process(pool_size=N)` on the block,
`[defaults] pool_size` for the project, or `[limits] max_processes_per_block` as
a ceiling. A body that is not helped by separate address spaces can take
`backend=thread` instead, as can any block on a free-threaded build.

---

## 3. Performance gaps

Here Lucen is correct but slower than it could be, because the gate or the
routing is conservative or a faster path is not yet the default. None of these
affect output.

### 3.1 `typed_buffers` is not in the cost model

The experimental `typed_buffers` flag ships typed result slabs (array or
bytearray) back from process workers, roughly an order of magnitude cheaper than
a list of the same floats. The profitability gate does not yet account for that
cheaper transfer, so it routes array-output maps to sequential even where the
typed process path would win.

For a dense array-output map, enable the flag and force the backend:
`activate(experimental=["typed_buffers"])` plus `# LUCEN START backend=process`.
Teaching the cost model the typed transfer cost is [ROADMAP](ROADMAP.md) N1.

### 3.2 Light reductions carry a small probe overhead

A trivial reduction over a large input carries roughly five to ten percent probe
overhead relative to sequential, because reductions cannot yet use the
twin-probe fast path that pure maps use: a reduction's sequential twin is
functional rather than in-place, so probing it still needs a chunk-function
slab. Nothing is required of you; if a specific light reduction is hot,
`calibrate=false` forces the parallel path. A reduction twin-probe is
[ROADMAP](ROADMAP.md) N2.

### 3.3 The recognized-DAG wavefront runs sequentially by default

A recognized-DAG block (`results[i] = combine(results[i // 2], ...)`) has a
level-synchronous parallel form (spec 5.8), but it runs sequentially by default:
on a GIL build one pickled dispatch per level loses badly. On a free-threaded
build, add `backend=thread` to a recognized-DAG block heavy enough to benefit.
Making the gate reach for it there is [ROADMAP](ROADMAP.md) M1.

### 3.4 No native core on free-threaded builds

The native accelerator ships as one `abi3` wheel that loads on GIL builds 3.9
through 3.14. The stable ABI and the free-threaded ABI are mutually exclusive,
so on a free-threaded interpreter `pip install lucen` selects the published
`py3-none-any` wheel and Lucen runs its pure-Python fallback. Only the
acceleration is absent, and the impact is small: the native core accelerates two
orchestration primitives, not the loop body or the dispatch, and the
free-threaded build posts competitive benchmark numbers without it. A separate
non-`abi3` build is [ROADMAP](ROADMAP.md) M2.

### 3.5 The loop body itself is never compiled

Lucen parallelizes the loop; it does not compile the loop body. Every iteration
runs the same interpreted bytecode it would run sequentially, on whichever
worker executes it, so for a body that is interpreter work over Python objects
the speedup is bounded by the core count and the interpreter rather than by
native code generation. Native compilation of a provably-typed numeric subset is
[ROADMAP](ROADMAP.md) L1, the flagship item, and is compiler-scale work.

---

## 4. Scope limits

Boundaries of the design, not defects.

### 4.1 One block per pragma pair, one construct per block

A `# LUCEN START` / `# LUCEN END` pair marks exactly one `for` loop, or one
assignment of a list, dict, or set comprehension. The body may contain
arbitrarily nested control flow, but the marked construct itself is a single
loop or comprehension over a sized iterable. A marked `while` has no iteration
space to chunk and runs as unmodified Python, as do `async` bodies and `async`
comprehensions.

A bare generator expression is never parallelized. It is lazy: nothing is
computed where it is written, so producing its elements at the marked line would
change when the work happens, force the whole result into memory, and diverge
outright if the consumer takes only a prefix or mutates the source first.

`total = sum(elt for t in it)` is the exception, because `sum` provably drains
the generator at that line and keeps none of it. Only the elements are
parallelized; they land in one positional list and `sum` itself then runs over
that list, sequentially, after the join. Reusing the builtin is what keeps the
result exact, since `sum` carries a compensation term for floats on CPython 3.12
and newer and a `+` fold over the same elements would return different bits.
Further `for` clauses are refused: only the outermost iterable is chunked, so
each slot would hold a whole row rather than an element.

### 4.2 Sized iterables only

Chunked dispatch requires a known length. A marked loop over an unsized iterable
takes the `UnsupportedIterableError` fallback and runs sequentially. Materialize
it to a list first if you want it parallelized.

### 4.3 `break` and `return` are sequential outside an experimental flag

A block containing `break` or `return` runs sequentially unless the experimental
early-exit scheduler is enabled (`activate(experimental=["early_exit"])`), which
reproduces sequential first-break semantics speculatively.

### 4.4 Not a substitute for vectorized kernels

Lucen accelerates Python-level loop bodies of meaningful size. A loop that
should be a single NumPy expression will be faster as that expression than as a
parallelized Python loop, and Lucen does not claim otherwise.

### 4.5 No guaranteed speedup

The profitability gate is a bounded-cost estimator, not a promise. It can
mispredict; when it does, the cost is bounded at roughly one chunk of
suboptimal scheduling per calibration cycle, it is visible in `lucen profile`,
and it never changes output.
