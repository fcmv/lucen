# Lucen Technical Specification

**Status:** Current, describes the shipped system.
**Scope:** Python, the whole library.
**Companion:** [`lucen_engineering_doc.md`](../implementation/lucen_engineering_doc.md), the contributor-facing map of how the code is organized.

This document is the decision record. It states every semantic and every
invariant Lucen holds, and it is the authority a code comment citing
`spec 5.x` points at. Where this document and the code disagree, one of them
is a bug; file it, do not resolve it silently.

The organizing idea: every invariant here constrains *results*, not
*mechanism*. "Never an incorrect result", bit-identical reductions, the
Comment Invariant, naive and expert parity, quiet fallback. None of them
require per-element bookkeeping. They are upheld by construction at a
per-chunk price: the chunk is the unit of dispatch, serialization, audit,
and commit, and all overhead is paid once per chunk and amortizes toward
zero per iteration.

---

## 1. Goals and Non-Goals

### 1.1 Goals

- **Ordinary Python, no new syntax.** Parallelism is marked with comment
  pragmas; the marked file is valid Python whether or not Lucen is present.
- **One-time activation.** `import lucen; lucen.activate()` installs an
  import hook; nothing else in the program changes.
- **Never an incorrect result.** No tier, no opt-out, ever. A parallel run is
  bit-identical to the same file run as plain sequential Python, or it does
  not happen.
- **Never disruptive.** Anything Lucen cannot prove safe runs as the
  sequential Python you wrote, and the reason lands in a structured fallback
  report rather than on stderr. `[errors].mode = "hard"` is the opt-in that
  turns fallbacks into failures.
- **Runs on standard CPython, exploits what the interpreter offers.** GIL
  builds 3.9 through 3.14 and free-threaded 3.13t/3.14t are all supported.
- **Never silently pointless.** A block that is parallel-eligible but
  predicted to lose to dispatch overhead runs sequentially instead and says
  so through the fallback channel (§5.17). This is a reported best-effort
  behavior, not an invariant: prediction can be wrong, the cost of a wrong
  prediction is bounded at one chunk, and it never changes output.
- **Explainable.** `lucen explain` reports every block's classification
  statically; `lucen profile` reports what actually ran (§5.15).
- **Naive and expert parity.** Every clause trades a proof for a named
  assertion, or exactness for speed, never a different unrequested result
  (§14).
- **Optional native core.** The Rust extension is a performance accelerator
  with an identical-semantics pure-Python twin for every operation. Lucen
  runs correctly and passes its whole suite with the extension absent, on any
  supported interpreter.

### 1.2 Non-Goals

- **Unmarked parallelization.** Lucen only touches marked blocks.
- **Automatic interprocedural verification of arbitrary callables.** Purity is
  proven where the source is readable (§5.4); beyond that the trust model
  applies.
- **Stable `break`/`return` parallelism** outside the experimental early-exit
  scheduler (§5.9.1).
- **Competing with vectorized native kernels.** Lucen accelerates
  Python-level loop bodies of meaningful size. A loop that should be a NumPy
  expression is not Lucen's job, and no output implies otherwise (§16.4).
- **Guaranteed speedups.** The profitability gate is a bounded-cost estimator,
  not a promise (§5.17).
- **Native loop-body compilation.** Compiling the marked loop body itself to
  native code is roadmap, not a current feature (§12).

---

## 2. System Requirements

- **Python:** CPython 3.9 or later. Free-threaded builds (3.13t, 3.14t) are
  supported on the pure-Python path.
- **Backend choice is decided live per call** via `sys._is_gil_enabled()`.
  The check must stay live because on a free-threaded interpreter, importing
  any C extension not declared free-threading-safe silently re-enables the
  GIL for the whole process. A backend cached at import time would be wrong
  for the rest of that process; the per-call check reroutes correctly.
- **Native core:** Rust with PyO3, built as a single `abi3` wheel that loads
  on every GIL build from 3.9 through 3.14. The stable ABI and the
  free-threaded ABI are mutually exclusive, so the `abi3` binary does not
  load on a free-threaded interpreter; there Lucen runs its pure-Python
  fallback, which is complete and tested. A free-threaded native build is
  roadmap (§12).
- **Serialization:** `pickle` protocol is available on all supported
  interpreters. The process backend uses it; the thread backend never
  serializes.

---

## 3. Terminology

| Term | Meaning |
|---|---|
| **Marked block** | The `for` loop bounded by `# LUCEN START` and `# LUCEN END`. |
| **Chunk** | A contiguous sub-range of the iteration space; the unit of dispatch, serialization, audit, and commit (§5.7.1). |
| **Chunk function** | The module-level generated function that runs one chunk as a plain Python loop (§5.16). |
| **Sequential twin** | The generated function that runs the block exactly as the original loop; it is both the fallback path and the pure-map probe (§5.16, §5.17). |
| **Slab** | A chunk's private output buffer (a preallocated list, a chunk-local dict, or a typed buffer), committed at join (§5.7.2). |
| **Commit** | Applying slabs to the real container at join, in chunk index order (§5.7.2). |
| **Write-set audit** | The join-time disjointness check that upholds correctness for asserted-safe shapes (§5.7.3). |
| **Level / wavefront** | One stratum of a recognized-DAG block; wavefront execution runs levels in order, each a flat parallel-for (§5.8). |
| **Probe** | The measured run of chunk 0 that decides profitability (§5.17). |
| **Backend** | `THREAD`, `PROCESS`, or `SEQUENTIAL`. The wavefront is a driver over THREAD/PROCESS, not a fourth backend. |
| **Trusted function** | A callable the user asserts is parallel-safe, via `# LUCEN TRUST`, `trust=callables`, or `[trust]` config (§5.3.4, §5.4). |
| **PARALLEL_UNPROFITABLE** | The reported outcome for an eligible block predicted to lose to sequential execution (§5.17). |

---

## 4. Architecture Overview

```
 source file
     |
     v
 [bytes prefilter]        no b"LUCEN" in file, untouched passthrough (§5.1)
     |
     v
 [Pragma Scanner]         tokenize-based; unconditional ClauseValueError (§5.2)
     |
     v
 [AST Rewriter]           six-way variable classification + audit-tier tag (§5.3, §5.4)
     |
     v
 [Dependency Analyzer]    five closed dependency forms (§5.5)
     |
     v
 [Selector + cost model + cache]   eligibility ladder (§5.6) + profitability pre-screen (§5.17)
     |
     v
 [Code Generation]        chunk function + sequential twin, per the canon (§5.16)
     |
     v
 [Import Hook]            content-and-version-keyed rewrite cache
     |
     v        +------------- at CALL time -------------+
 [Preflight Gate §5.13]   cheap checks; purity gate; first-chunk pickle convergence (PROCESS)
     |
 [Calibration probe §5.17]  chunk 0 measured; twin-probe for pure maps
     |
 [Chunked dispatch]       flat (§5.9/§5.10) or wavefront driver (§5.8)
     |
 [Join + write-set audit §5.7.3]
     |
 [Commit, chunk order §5.7.2]  reduction fold per reduce= (§5.12)
```

Any audit conflict or mid-run serialization failure discards all uncommitted
slabs and transparently re-runs the block sequentially (§5.14), possible
precisely because nothing commits before join. `lucen explain` runs only
Scanner through Selector; `lucen profile` additionally observes the probe,
dispatch, and audit stages (§5.15).

---

## 5. Component Specifications

### 5.1 Activation and Import Hook

`activate(experimental=[...])` installs a front-of-`meta_path` loader scoped
by `[scope]` config. It is idempotent. `deactivate()` uninstalls the hook;
modules already imported keep their rewrites.

- **Bytes prefilter:** before tokenizing, the loader scans the raw source
  bytes for `b"LUCEN"`. Absent, the file loads exactly as CPython would,
  with no tokenizer and no AST. A prefilter may only ever false-positive (a
  stray occurrence pays one tokenize pass and finds no pragma); it can never
  false-negate, because every real pragma contains the token.
- **Rewrite cache:** keyed by source content hash, Lucen version, and
  interpreter version, so a warm import skips scan, rewrite, analyze, and
  codegen. Written atomically and readable by PROCESS-backend children.

### 5.2 Pragma Scanner

Tokenize-based, so pragma-looking text inside a string or a docstring is
never mistaken for a pragma. The grammar is `LUCEN (START|END|TRUST)`
optionally followed by clauses. A `START` clause with a malformed or
out-of-range value raises `ClauseValueError` unconditionally, independent of
`[errors].mode`: a malformed pragma is a programming error in the marked file,
surfaced while the Scanner is actively parsing, never at runtime. Unterminated
blocks, lowercase spellings, and pragmas inside strings are handled without
error. The clause registry the Scanner validates against is defined in §7.

### 5.3 AST Rewriter and Variable Classification

The rewriter classifies every name referenced in the block against the outer
loop's data flow. The body may contain arbitrarily nested `if`/`elif`/`else`,
`while`, `for`, and `try`/`except`/`else`/`finally`; safety is decided by the
outer loop, not by control-flow depth. The marked block itself must be a
`for` loop over a sized iterable (§6).

The six classifications: `LOOP_LOCAL` (written before read on every path
within an iteration), `OUTER_READONLY` (read, never written),
`SHARED_INDEXED_SAFE` (written at a provably distinct index),
`READ_AFTER_WRITE` (a cross-iteration read of a written container),
`SHARED_INDEXED_UNRESOLVED` (written at an index the analyzer cannot resolve),
and `SHARED_SCALAR` (a reduction accumulator or an unresolved shared scalar).

A `LOOP_LOCAL` temporary is recognized even when its dominating first write
sits inside a nested scope; a name that is only conditionally written and
escapes the loop is treated as shared. An inner-loop `break` is not mistaken
for an early exit of the marked loop.

Escaping lambdas and generator expressions inside a block are rejected: they
close over loop state a parallel chunk cannot reproduce. Both remain legal as
eager call arguments.

#### 5.3.3 Conservative branch-merge

When a name is written on some branches of an `if` and not others, and the
merge is ambiguous, the block is conservatively routed sequentially. The
experimental `branch_sensitive_deps` flag opts into per-branch dependency
classification: a write-only branch-merge conflict runs parallel under the
tier-C write-set audit (§5.7.3) and re-runs sequentially if a real
cross-branch overlap is detected. Writes to the same provably-distinct index
across branches are safe without the flag.

#### 5.3.4 Trusted functions

A callable named in a `trust=` assertion, a `# LUCEN TRUST` pragma, or the
`[trust]` config is treated as parallel-safe: its arguments are classified as
reads, and the purity proof (§5.4) does not downgrade the block on its
account. A trusted name that resolves ambiguously (two different callables
under one name in scope) raises `AmbiguousTrustedNameError`. Trust is the
user's assertion; §5.4 states what Lucen proves without it.

### 5.4 Indexed Container Handling and Callable Purity

**Audit tiers.** Only the bare loop variable or an `enumerate()` index counts
as a provably distinct index for `SHARED_INDEXED_SAFE`. The distinction
selects the audit tier (§5.7.3):

- **Distinct by proof:** integer indices produced by `range()`/`enumerate()`.
  Distinctness is a construction fact. Tier A.
- **Distinct by assumption:** the loop variable's value used as a key
  (`d[item] = ...` while iterating `items`). Distinct only if the iterable has
  no duplicates, which is not statically knowable. The classification stands
  and the runtime check is load-bearing: tier B catches a duplicate as a write
  conflict, which discards and re-runs sequentially for a last-write-wins
  result identical to sequential (§5.14).

**Callable purity proof.** A helper the analyzer can read is checked for
side effects on hidden state. A helper *proven* to mutate a module global, a
closure cell, `random.*` state, or to call an I/O builtin such as
`print`/`open`, transitively through calls up to a depth cap, makes its block
run sequentially with a report naming the helper and the reason. Only positive
proof downgrades a block: pure Python helpers, C extensions, and anything the
analyzer cannot resolve keep the documented args-as-reads trust, so existing
parallel routing is never changed by this check. This closes the silent
per-worker divergence of a stateful helper on the process path, and it makes
side-effect order sequential wherever the effect is detectable: an
I/O-calling block runs in loop order by construction rather than being
downgraded for order alone.

Overrides, narrowest first: `# LUCEN TRUST` on the helper's `def`,
`trust=callables` on the block, or `[trust] callables` in `lucen.toml`.

### 5.5 Dependency Analysis

Five closed forms, recognized structurally, never by running code, with the
false-positives-never discipline: a shape is recognized only when its
structure proves the property.

#### 5.5.1 Self-contained

No cross-iteration dependency. Parallelizes as a flat chunked map.

#### 5.5.2 Monotonic offset

`results[i - k]` for constant `k > 0`: a strict cross-iteration chain. Always
sequential, reported informationally. This is not an error; it is a shape the
analyzer recognizes precisely so it can explain the refusal.

#### 5.5.3 Recognized DAG

`results[i // c]` for constant `c > 1`, including the shorthand forms
normalized into it (`i >> k` becomes `i // 2**k`). Every dependency index is
provably smaller than `i`, which yields a level decomposition (§5.8).

#### 5.5.4 Recognized reduction

A `SHARED_SCALAR` accumulated by a recognized associative-in-order operator
(`+`, `*`, `min`, `max`, `&`, `|`, `^`, or a declared `reduce=custom`). Runs
parallel with per-chunk partials folded in chunk order (§5.12). An accumulator
shape the analyzer does not recognize is sequential.

#### 5.5.5 Modular self-reference

`results[(i + k) % n]`: a potential cycle. Always sequential.

Anything outside these forms is `UNRESOLVED` and runs sequentially unless the
user asserts `depend=none`, which engages the tier-C audit (§5.7.3).

### 5.6 Backend Selection Ladder

| Condition | Eligibility |
|---|---|
| `SHARED_INDEXED_SAFE` / self-contained | Parallel-capable; backend per §5.6.1 |
| Recognized DAG shape | Wavefront driver (§5.8), else SEQUENTIAL |
| Recognized reduction shape | Parallel-capable; backend per §5.6.1 |
| No recognized merge shape for a shared scalar or unresolved index | SEQUENTIAL |
| Monotonic / modular self-reference / unresolved shape | SEQUENTIAL (subject to §5.14) |
| Block contains `break`/`return` | SEQUENTIAL unless experimental early-exit (§5.9.1) |
| Nested-region guard hit | SEQUENTIAL for the inner region (§5.11) |
| Eligible but predicted unprofitable (§5.17) | SEQUENTIAL, reported `PARALLEL_UNPROFITABLE` |

#### 5.6.1 Interpreter-independent routing

Backend selection is decided by the block's data shape, not by the
interpreter. Measurement on a free-threaded build showed THREAD is
catastrophic for the shapes Lucen parallelizes: reading a shared container
contends on its refcount, and writing a shared list serializes on that list's
per-object mutation lock. PROCESS has neither. So maps and reductions route to
PROCESS on both GIL and free-threaded builds; the cost model costs the chosen
backend, not the interpreter.

THREAD stays reachable for the cases where a process copy would lose the
result or cost more than it saves, and for the expert override:

- **No output to ship back** (a block whose only effect is in-place mutation
  or a side effect through a reference): PROCESS would copy the work and drop
  the mutation, so THREAD is chosen.
- **Un-sliceable structured reads** (a whole sub-container read per iteration
  where the container cannot be sliced per chunk): PROCESS would ship the
  whole container to every chunk, so THREAD is chosen.
- **Explicit `backend=thread`**: honored as written.

On a free-threaded build only, a block whose measured per-iteration cost
clears a conservative floor is promoted from PROCESS to THREAD: once the body
is heavy enough that contention is amortized, THREAD edges PROCESS by not
marshalling. A forced or explicit backend is never overridden. The
recognized-DAG wavefront runs sequential-by-default on both builds; its
parallel form is reached by an explicit `backend=thread` on a free-threaded
build.

### 5.7 Execution Model: Chunks, Slabs, Commit

#### 5.7.1 Chunking

The iteration space is partitioned into contiguous index ranges. The default
chunk count is tuned per backend: THREAD uses more chunks for load-balancing
slack (a thread dispatch is cheap); PROCESS uses fewer, because a process
dispatch costs roughly an order of magnitude more and oversubscription spends
real milliseconds on round-trips. Explicit control is `backend=...(chunks=N)`.
Chunked dispatch requires a sized iterable; an unsized iterable takes the
`UnsupportedIterableError` fallback path.

#### 5.7.2 Slabs and chunk-ordered commit

Each chunk writes into a private slab: a preallocated list for list output, a
chunk-local dict for keyed output, per-chunk partials for reductions. Inside
the chunk function there is zero interception; a plain `slab[j] = ...` stores
at Python speed. At join, slabs commit to the real container in chunk index
order: one slice-assignment or one ordered dict-merge per chunk. Four
properties, each doing invariant work:

1. **Zero per-iteration overhead** for zero-clause proven-safe blocks.
2. **Dict insertion order is bit-identical to sequential.** Within-chunk order
   is preserved by the chunk-local dict; cross-chunk order by the chunk-ordered
   merge. Distinct keys are enforced by the audit; a duplicate is a conflict
   and re-runs sequentially.
3. **Error post-state equals the sequential prefix.** If chunk *k* raises under
   the fail-fast default, chunks `0..k-1` commit and chunk *k* onward are
   discarded, leaving the container exactly as sequential execution would at
   the same failure. The buffer and direct-write fast paths document their
   narrower guarantee (§5.7.4).
4. **Transactional discard needs no rollback.** Nothing commits before join,
   so the discard-and-re-run response to any conflict has no partial state to
   clean up.

#### 5.7.3 Write-set audit

Join-time disjointness verification, tiered by what was proven:

| Tier | Applies to | Mechanism | Per-write cost |
|---|---|---|---|
| **A** | Distinct-by-proof integer indices (§5.4) | The write range is the chunk's lease; the audit verifies range and count | Zero |
| **B** | Distinct-by-assumption keys (§5.4) | Per-chunk written-key set; join intersects sets | One `set.add` |
| **C** | User-asserted safety (`depend=none` on an unresolved integer index) | Per-chunk index bitmap; join ORs bitmaps, any overlap is a conflict | One bit-set |

Any detected overlap raises `ParallelWriteConflictError`: discard, transparent
sequential re-run, or immediate raise under `hard`. `skip_runtime_check=true`
disables tiers B and C.

What the audit does not catch, stated plainly: an undeclared cross-iteration
*read* under a false `depend=none` assertion. Asserted safety is the user's
assertion. One note in the user's favor: slab privatization makes even a false
assertion deterministic (reads see the pre-block state, never another chunk's
in-flight writes), so it is reproducibly wrong rather than racy. It takes two
explicit assertions, `depend=none` and `skip_runtime_check=true`, to reach a
silently wrong result; one assertion alone is always caught by the audit.

The tier-B/C audit runs as a single native call over all chunks when the
extension is present (§5.7.5), and a pure-Python set merge otherwise.

#### 5.7.4 Buffer-protocol and direct-write fast paths

Two shapes skip the slab-plus-commit and write disjoint sub-ranges of the real
output directly, dispatching the sequential twin across the pool:

- **Buffer output** (`array.array`, `bytearray`, writable memoryview) from a
  dense tier-A map: workers write disjoint slices of the real buffer; commit is
  a slice copy.
- **Builtin-list output** from a tier-A proven map: indices are proven
  disjoint, so the block writes disjoint sub-ranges directly rather than paying
  a slab plus ordered commit.

Three boundaries are load-bearing and keep the invariant story intact:

- A list *subclass* keeps the slab: its `__setitem__` is observable user code
  that the commit path must call single-threaded, in order.
- A block with `on_error`, `timeout`, or `progress` keeps the slab: that
  per-iteration instrumentation lives in the chunk function, which the direct
  path's bare twin does not carry.
- The documented mid-error caveat: on the direct path, later-index writes may
  already be present in the container. `explain` states this when the fast
  path applies.

#### 5.7.5 What runs in the native core

The optional Rust core (`lucen._core`, behind the `lucen._accel` seam)
runs the orchestration loops that are hot at Python level and have an exact
native equivalent:

- **Write-set audit** (`audit_index_bitmap`): the whole tier-B/C audit over
  every chunk's key list in one native call. Word-wise bitmap OR; the per-index
  cost is a word test rather than a Python-to-native method call.
- **Ordered reduction fold** (`fold_ordered`): the contribution fold walked in
  the exact order the sequential loop uses (outer element index, inner site
  order), combining *by reference* through CPython's own number protocol and
  rich comparison. Because it calls the same C-level operations the interpreter
  calls for `+`, `*`, `min`, and the rest, the result is the sequential loop's
  for every operand type: float bits, unbounded integers with no wrapping,
  user-defined operators. No operand is converted or copied; only the
  per-element bytecode dispatch is eliminated.

Every native operation has an identical-semantics pure-Python twin, selected
by the `lucen._accel` seam when the extension is absent or
`LUCEN_DISABLE_NATIVE` is set. Two things are deliberately *not* native and
the measurements are recorded in the code:

- The **element-wise slab commit** was measured slower in native code than
  CPython's `zip` plus specialized list stores, and stays Python.
- A **data-marshalling f64 fold** (copying boxed floats across the boundary)
  was a net loss and was retired in favor of the by-reference fold above.

Native loop-body compilation is roadmap, not this seam: the core moves the
orchestration, not the loop body (§12).

### 5.8 Level-Synchronous Wavefront Execution

For the recognized-DAG shape, the recognition rule supplies the schedule.
Since every dependency satisfies `dep(i) = i // c <= i / c` with constant
`c > 1`:

- **Level decomposition:** level *k* is the index range `[c**k, c**(k+1))`
  plus the base indices with no dependency. For any `i` in level *k*, its
  dependency is strictly below `c**k`, so it lands in an earlier level.
- **Execution:** levels run in ascending order, each an ordinary contiguous
  parallel-for on the flat scheduler (§5.9/§5.10), with a barrier and slab
  commit between levels. Reads in level *k* hit already-committed values with
  zero interception. Levels narrower than `grainsize=` run inline in the
  caller; the early levels are tiny by construction and the last level holds
  most of the elements, so parallelism is widest where the work is.
- **Scale:** `n = 1M, c = 2` gives about 20 dispatches, not one task per index.
- **Deadlock-freedom:** trivial. No task ever waits on another task; the only
  synchronization is the level barrier.
- **`depend=acyclic(order=<expr>)`** generalizes the same driver: evaluate the
  user's order key per index in one pass, bucket by key, run buckets in
  ascending order with equal keys parallel within a bucket. The user asserts
  the keys respect true dependencies.
- **PROCESS wavefront:** each level is an ordinary batch map, so the DAG shape
  runs on a GIL build without fine-grained cross-process waiting. It is
  cost-gated (§5.17): on a GIL build the wavefront runs sequential-by-default
  unless the user forces `backend=process`, because one pickled dispatch per
  level loses badly to sequential for typical shapes.

### 5.9 Flat Chunk Scheduler (THREAD)

Scoped to never-blocking shapes, dispatching chunks (§5.7.1):

- **THREAD never serializes.** Arguments pass by reference; picklability is
  irrelevant on this backend.
- `on_error=` applies at chunk granularity; the default fail-fast surfaces the
  error from the lowest failing iteration index, matching sequential's
  fail-fast contract; not-yet-started chunks are cancelled; §5.7.2(3) defines
  the post-state.
- `break`/`return` must never reach this scheduler; the Selector routes them
  out. The scheduler asserts this rather than silently handling it.

#### 5.9.1 Experimental Early-Exit Scheduler

Opt-in `experimental=["early_exit"]`. `break`-bearing flat blocks run
speculatively; the committed result is the prefix up to the lowest break plus
that iteration's own pre-break writes, matching sequential first-break
semantics. Chunks entirely above the exit are discarded unstarted where
possible; the chunk containing the exit truncates its slab at the exit offset;
the join waits only for chunks below the exit.

### 5.10 Process Backend Scheduler

Built around persistent-pool economics.

- **Persistent lazy pool:** created on the first PROCESS-eligible call, reused
  across all blocks for the process lifetime, torn down atexit. Recycle events
  (for example a `timeout=` hard-kill, §7) respawn workers and are reported
  through the fallback channel as a documented cost.
- **Spawn-safety scan:** on a spawn platform, an entry script that does work at
  import time without an `if __name__ == "__main__"` guard is detected in the
  parent, before any worker spawns, by an AST check of the entry module. The
  block runs sequentially with an actionable message instead of the child-side
  `RuntimeError` flood that multiprocessing would otherwise produce. Guarded or
  effect-free entry scripts, and REPLs, spawn normally.
- **Per-chunk input slicing:** a range-domain parameter read only as `xs[i]`
  ships to each worker as the slice that chunk indexes, wrapped in an offset
  view, not the whole array per chunk. This cuts input transfer from
  O(chunks * n) to O(n). A parameter used any other way (`len(xs)`, `xs[i-1]`,
  `xs[j]`, or written) ships whole, so a result can never change. The same
  slicing applies to a structured read (`for v in rows[i]`, `rows[i][k]`) when
  `rows` is read only as `rows[i]`.
- **Single-pickle payload:** each chunk's argument bundle is pickled exactly
  once. The serialization proof (§5.13) ships as the payload; bytes re-pickle
  as a memcpy in the pool submit, rather than being thrown away and re-pickled.
- **Exception rehydration:** a worker exception ships the exception type's
  identity (module, qualname, message). The parent reconstructs the real
  exception type, so a caller's `except UserError:` handler catches it as it
  would under sequential execution. Only if reconstruction fails does an
  annotated `RuntimeError` fallback remain.
- **Payload reconstruction failure:** a worker that cannot unpickle a bundle
  (for example, referencing a module whose directory joined `sys.path` after
  the pool spawned) raises `MidRunSerializationError` to the parent, which
  recycles the pool and re-runs sequentially.
- **Reductions:** per-chunk local partials, combined in the parent per
  `reduce=`/`reduction_order=` (§5.12). Chunks are index-contiguous, so the
  fold is linear in chunk order, bit-identical, negligible cost.

### 5.11 Nested Parallel Region Handling

A context flag marks an active dispatch. A marked block reached from inside an
already-running marked block runs its inner region sequentially, silently, not
tiered by `[errors].mode`; the `nested=` clause governs reporting and the
reserved hard-fail opt-in only. A recursion-headroom guard (§5.13) is a
separate protection against depth exhaustion.

### 5.12 Resume and Reassembly

Commit is chunk-ordered, always (§5.7.2). Reduction combine per
`reduction_order=`:

- `sequential_equivalent` (default): a linear fold of per-chunk partials in
  chunk index order, bit-identical to sequential. The fold has as many terms
  as chunks, so its cost is negligible regardless of n. This fold runs
  natively by reference when the extension is present (§5.7.5).
- `stable`: a reproducible tree-combine is permitted (the mode where combine
  parallelism can win).
- `custom(combine=)`: user callable; smoke-tested at the Gate; associativity
  is the user's assertion. A custom callable always runs the Python fold.

Parallel partial-sum reductions re-associate float addition and are therefore
*not* bit-identical to a left-to-right sequential fold. Lucen does not do
this: it folds per-element contributions in sequential order, which is why its
reductions match sequential bit for bit on every interpreter.

### 5.13 Preflight Gate

**The promise:** nothing is deferred past the Gate whose later failure could
disrupt the program or leave an incorrect or partial result. A deferred check
may only fail into the same transparent discard-and-sequential-re-run path the
runtime already guarantees.

| Check | Catches | Failure behavior |
|---|---|---|
| Live GIL check | Picks THREAD vs PROCESS from eligibility | Not a failure case |
| Purity gate (§5.4) | A helper proven to mutate hidden state | Runs SEQUENTIAL, reports `PreflightCheckError` naming the helper |
| Recursion-headroom guard | Dispatch/pickle depth blowing a user-lowered `setrecursionlimit` | Runs the sequential twin below a spare-frame floor, reports `RecursionHeadroom` |
| First-chunk pickle convergence (PROCESS only) | A value-shifting `__reduce__`/`__getstate__` | Fallback per §5.14 before dispatch; `hard` raises |
| Spawn-safety scan (§5.10) | An unguarded entry script on a spawn platform | Runs SEQUENTIAL with an actionable message |
| Custom reduction smoke test | A broken `reduce=custom` identity | Fallback per §5.14 |
| Pool availability probe | Environments where a pool cannot start | Fallback per §5.14 |
| Nested-region guard (§5.11) | Already-active dispatch context | Silent SEQUENTIAL, untiered |
| Clause value validation | Malformed or out-of-range runtime clause values | Fallback per §5.14 |
| Profitability decision (§5.17) | Predicted-unprofitable dispatch | Not a failure; reported when it routes to SEQUENTIAL |

**Pickle convergence.** The first chunk's argument bundle must reach a byte
fixed point after one pickle round-trip: `dumps(loads(gen1)) == gen1`. An
accumulating `__reduce__`/`__getstate__` (the demonstrated silent-corruption
exploit) never converges, and the block falls back sequentially and loudly.
Generation 0 is deliberately excluded from the comparison: the parent's first
dump embeds string-interning identity that a load cannot reproduce, so an
honest bundle converges only from generation 1 onward. Override: `trust=pickle`.

Remaining chunks serialize fused with dispatch; a later heterogeneous failure
routes into the mid-run serialization fallback (§5.10, §5.14). The THREAD
backend never serializes.

**Named residual risk.** Under fail-fast, iterations later than a failing
iteration may have executed on other workers before cancellation. Their
results are discarded (the container post-state is the sequential prefix), but
their external side effects fall under the single named residual risk of
running arbitrary code concurrently at all. Scoped narrowly, not generalized
into a disclaimer.

### 5.14 Error and Fallback Philosophy and Catalog

Three axes (`on_error=`, `strict=`, `[errors].mode` plus `on_fallback=`),
three mode tiers (`report`, `quiet`, `hard`), once-per-block logging,
`get_fallback_report()`, and `ClauseValueError`'s unconditional exemption from
mode handling.

| Error | Raised by | Fallback (non-`hard`) | `hard` |
|---|---|---|---|
| `ParallelWriteConflictError` | Join-time audit (§5.7.3) | Transparent sequential re-run | Raises |
| `MidRunSerializationError` | PROCESS pipeline (§5.10) | Transparent sequential re-run | Raises |
| `PreflightCheckError` | Purity / preflight gate (§5.4, §5.13) | Runs SEQUENTIAL, reported | Raises under `strict` |
| `PARALLEL_UNPROFITABLE` (reason, not exception) | Selector / calibration (§5.17) | Runs SEQUENTIAL, reported once, allowlistable | Under `strict=true` without allowlist: raises |
| `ClauseValueError` | Scanner / config (§5.2, §8) | Always raised, mode-independent | Raises |

### 5.15 Diagnostics and Profiling CLI

#### 5.15.1 `explain`

Static per-block report. It reports facts as facts and anything knowable only
at call time (argument picklability, custom-callable well-formedness, pool
availability) never as a yes or no. It reports the profitability pre-screen as
an estimate, never a measurement, with the literal
`# LUCEN START calibrate=false` override. `--strict --baseline` fails a
build when a block's classification regresses against a committed baseline.

#### 5.15.2 `profile`

Runs a script and reports observed backends, workers, probe timings,
calibration decisions, fallbacks, and a per-block parallel efficiency figure
(observed speedup against workers used), the empirical surface for the
contention ceiling (§16.3), reported without claiming a cause.

#### 5.15.3 `run`

Rewrites the target script and executes it as `__main__`. Because plain
`python script.py` runs the entry module before `lucen.activate()` can install
the hook, a loop marked in the entry script itself is otherwise never rewritten;
`lucen run` closes that gap, so a single self-contained file parallelizes
without a separate importable module. The script's directory is placed on
`sys.path` and the hook is rooted there, so the modules the script imports are
rewritten too.

### 5.16 Codegen Performance Canon

The generated code is the hot path; these rules are tested, not stylistic.

1. **Two functions per block:** a module-level chunk function taking
   `(start, stop, <hoisted bindings>, <clause parameters>)` whose body is a
   plain `for` loop, and a sequential twin that runs the block exactly as the
   original loop. Module-level makes both spawn-safe; parameters rather than
   closures make both picklable and `LOAD_FAST`-fast.
2. **Hoist** every `OUTER_READONLY` global, attribute chain, and method to a
   parameter bound once at dispatch.
3. **Preallocate object slabs** and store positionally.
4. **Iterate the chunk range directly.**
5. **Zero closures, zero cells, zero globals** in the generated function.
6. **Clause-conditional instrumentation only.** Deadline reads (`timeout=`),
   progress ticks (`progress=`), and audit key-set adds (tier B/C) are emitted
   only when the clause or tier requires them. A zero-clause tier-A block
   compiles to the plain loop plus positional stores and nothing else. This is
   the enforcement point for the zero-interception budget and makes naive and
   expert parity hold for performance as well as output.
7. **Commit placement:** slab stores happen inside user code as plain stores;
   audit and commit run strictly after the user's `try`/`except` has exited, so
   no Lucen-internal exception is ever visible to a user `except` clause.

The sequential twin is also the fallback path, so the sequential behavior *is*
the original loop, and for a pure map it is also the profitability probe
(§5.17).

### 5.17 Profitability Gate and Calibration

Two estimators and a memo, invisible to output.

- **Static pre-screen (Selector, cached):** an AST op-weight estimate of
  per-iteration body cost against backend dispatch and commit constants. Only
  blocks that fail by a wide margin are classified `PARALLEL_UNPROFITABLE`
  statically; the static model's job is to catch the hopeless cases in
  `explain`, not to be precise.
- **Runtime probe:** chunk 0 is measured. Let `t` be measured per-iteration
  cost, `R` the remaining iterations, `W` workers, `O(k)` the dispatch and
  commit overhead for `k` chunks: dispatch the rest iff
  `t * R * (1 - 1/W) > O(chunks(R))`, else finish sequentially and report
  `PARALLEL_UNPROFITABLE`. The cost model costs the backend the interpreter
  will actually use (§5.6.1), weighing per-element pickle overhead on a GIL
  build so light million-element loops stay sequential while CPU-bound work
  routes to PROCESS.
- **Twin-probe for pure maps.** A pure map (list slabs, no reductions) is
  probed on the sequential twin itself, which writes output in place, so chunk
  0 needs no private slab and no commit copy. Reductions keep the
  chunk-function probe (their twin is functional, not in-place). This removes
  the probe's double-write on the case that most often ends up sequential.
- **Calibration memo:** per block, `t` and the decision are memoized;
  subsequent calls skip the probe while the memo is fresh, refreshing on a call
  count or on an iteration-count regime change.
- **Bounded misjudgment:** a wrong prediction in either direction costs at most
  about one chunk of suboptimal scheduling per calibration cycle, reported in
  `profile`.

**`calibrate=` clause (§7):** absent is auto (pre-screen, probe, memo);
`calibrate=false` never gates (always parallelize when eligible);
`calibrate=static` is pre-screen only; `calibrate=always` probes every call
with no memo; `calibrate=threshold(min_gain=<float>)` sets a custom break-even.
Every tier produces identical output; the clause trades time and observability
only.

---

## 6. Legal Syntax Subset

A marked block is a `for` loop over a sized (`len()`-able) iterable. Unsized
iterables take the `UnsupportedIterableError` fallback, because chunked
dispatch depends on a known length. The body may nest control flow freely
(§5.3); a marked `while` has no iteration space to chunk and falls back to
unmodified Python. One marked block per pragma pair, one loop per block, no
`async` bodies.

---

## 7. Pragma Clause Reference

The tiering rule: the absent (naive) form is silent and automatic; each step
up trades a specific proof for a specific named assertion, or exactness for
speed, never a different unrequested outcome (§14). Sixteen clause groups.
The rows that carry the most weight:

| Clause (host) | Absent | Named forms |
|---|---|---|
| `backend=` (`START`) | live ladder decides (§5.6) | `thread` / `process` / `sequential`; `process(chunks=M)`; `process(pool=<factory>)` |
| `calibrate=` (`START`) | auto (§5.17) | `false` / `true` / `static` / `always` / `threshold(min_gain=<float>)` |
| `trust=` (`START`) | purity proof decides (§5.4) | `callables` (trust all helpers in the block) / `pickle` (waive convergence, §5.13) / `all` |
| `depend=` (`START`) | analyzer decides | `none` (tier-C audit still runs, §5.7.3); `acyclic(order=<expr>)` (§5.8); `none, skip_runtime_check=true` (disables tiers B/C) |
| `grainsize=` (`START`, wavefront) | runtime default minimum level width | `<N>`; `<N>(min_workers=M)` |
| `timeout=` (`START`) | no bound | `<s>` whole-block (THREAD: cooperative deadline between iterations, a stuck iteration cannot be preempted; PROCESS: hard kill plus documented pool recycle); `<s>(per_task=true)`; `<s>(per_task=true, on_timeout=<callable>)` |
| `strict=` (`START`) | project default | `true`; `true(allow=[monotonic, unprofitable])` |
| `on_error=` (`START`) | fail-fast | `collect`; `custom(<callable>)` |
| `reduce=` (`START`) | recognized operator | `custom(fn=<callable>, identity=<value>)` |
| `progress=` (`START`) | off | `true`; `true(per_task=true)` |

`reduction_order=`, `nested=`, `on_fallback=`, `affinity=`, `pool_size=`, and
`chunks=` carry as documented. `start_method=` selects the pool start method
(Linux-only in practice; OS default otherwise). Callable-valued expert clauses
are never TOML-settable. A removed clause (`process_wait=`, `batch_size=`) is
rejected with a message pointing at its replacement.

---

## 8. `lucen.toml` Reference

Sections: `[defaults]`, `[limits]`, `[strict]`, `[errors]`, `[experimental]`,
`[trust]`, `[scope]`, `[observability]`, with the precedence chain
`built-in < [defaults] < pragma < [limits]`.

```toml
[defaults]
backend = "thread"
pool_size = 16
chunks = 0                 # 0 = auto per backend (§5.7.1)
calibrate = "auto"
on_error = "collect"
strict = false
reduction_order = "sequential_equivalent"
progress = false

[trust]
callables = ["mylib.fast_path"]   # names trusted parallel-safe (§5.3.4, §5.4)

[limits]
allow_experimental = true         # false vetoes every experimental flag
```

Config values get the same strictness as pragma clauses: `pool_size = 0`,
`chunks = -4`, or a non-positive `[limits]` ceiling is rejected loudly, not
tolerated. A malformed file raises `ClauseValueError` naming the file, not a
raw parser traceback. An unknown key is rejected outright, so a stale
`process_wait = true` errors clearly instead of doing nothing.

---

## 9. Worked Examples

### 9.1 A plain map

```python
# LUCEN START
for i in range(len(records)):
    scores[i] = score(records[i])
# LUCEN END
```

`SHARED_INDEXED_SAFE`, tier A, self-contained. Routes to PROCESS on a GIL
build (§5.6.1), input sliced per chunk (§5.10), output committed by disjoint
sub-range (§5.7.4). Bit-identical to the same file with the pragmas treated as
comments.

### 9.2 Recognized-DAG reduction with tuning

```python
# LUCEN START grainsize=1024, progress=true
for i in range(1, n):
    results[i] = combine(results[i // 2], weights[i])
# LUCEN END
```

Recognized DAG; `weights` is `OUTER_READONLY`. The wavefront driver runs
levels `[1,2), [2,4), ... [2**k, 2**(k+1))` in order; levels narrower than
1024 run inline; wider levels dispatch as chunked parallel-fors with a barrier
and commit between levels. For `n = 1M`, about 20 levels. Sequential-by-default
on a GIL build unless `backend=process` is forced.

### 9.3 A too-small block, gated honestly

```python
# LUCEN START
for i in range(len(xs)):        # small n, trivial body
    ys[i] = xs[i] * 2 + 1
# LUCEN END
```

```
Block 1 (line 1)
  - Sequential (parallel-eligible; predicted unprofitable)
  Reason: SHARED_INDEXED_SAFE, eligible. Estimated body cost is below the
  dispatch break-even (§5.17). Estimate, not a measurement; the runtime probe
  re-checks against real timings.
  Suggestion: to force parallel dispatch, add:
    # LUCEN START calibrate=false
```

---

## 10. Build and Packaging

A single `abi3` wheel covers CPython 3.9 through 3.14 GIL builds; the
pure-Python fallback covers free-threaded builds and any interpreter without
the extension. Built with maturin over a PyO3 crate declared for the stable
ABI. The `lucen` console entry point is the CLI (§5.15). CI builds and
tests across Linux, macOS, and Windows, re-runs the accel parity tests under
`LUCEN_DISABLE_NATIVE=1`, and includes a free-threaded leg.

---

## 11. Testing Requirements

- **Cross-backend equivalence:** every workload bit-identical across
  sequential, thread, and process, and against the pragma-as-comment baseline.
- **Zero-interception proof:** for zero-clause tier-A blocks, assert on the
  generated code (no audit calls, no `LOAD_GLOBAL` in the hot loop) and on
  timing against a hand-written loop.
- **Error post-state:** a mid-block error leaves the container exactly equal to
  the sequential prefix, across backends (§5.7.2(3)).
- **Dict order and duplicate keys:** chunk-ordered merge reproduces sequential
  insertion order; a duplicate loop value triggers the tier-B audit and the
  re-run equals sequential last-write-wins.
- **Wavefront properties:** randomized `(n, c, grainsize, workers)` equivalence
  against sequential; every dependency of level *k* lands in a level below *k*
  for the full recognized vocabulary including shorthand forms.
- **Calibration decisions:** synthetic timing matrices to documented decisions;
  memo refresh triggers; the one-chunk misjudgment bound.
- **Mid-run serialization recovery:** first-chunk failures fire pre-dispatch; a
  late heterogeneous failure produces output byte-identical to sequential via
  the transparent re-run, and raises under `hard`.
- **Native parity:** every native operation returns exactly what its
  pure-Python twin returns, including bignum folds with no wrapping, float
  bit-identity, min/max tie-keeping, SKIP gaps, multi-site fold order, and
  hostile `__setitem__` call counts. The whole suite passes under
  `LUCEN_DISABLE_NATIVE=1`.
- **Adversarial corpus:** the red-team scenarios (aliasing, dependencies,
  ordering, error semantics, hostile containers, malformed pragmas, config
  poisoning) either run bit-identically or refuse loudly.

---

## 12. Roadmap

| Item | Why deferred |
|---|---|
| **Native loop-body compilation** | The flagship performance item. Compile marked bodies in a provably-typed numeric subset (arithmetic and `math.*` over uniformly-typed containers) to native kernels behind the same prove-or-fallback gate; anything outside the subset keeps today's path. Bignum overflow must fall back to keep integer results exact, and exception semantics must keep the sequential prefix and the exact type. Compiler-scale work. |
| Free-threaded native core | The `abi3` binary cannot load on a free-threaded build (the stable ABI and the free-threaded ABI are mutually exclusive). Shipping the core there needs PyO3 with free-threaded support, a separate non-`abi3` `cp3xt` wheel, an explicit GIL-free module declaration (without it CPython silently re-enables the GIL on import), and a thread-safety audit of the native entry points. Measured upside on the two current primitives is small, so it sits behind higher-value work. |
| `typed_buffers` in the cost model | The gate does not yet route array-output maps to the typed PROCESS path automatically; it currently needs `backend=process` plus the flag. |
| Reduction twin-probe | Reductions cannot use the twin-probe fast path yet, so light reductions carry a small probe overhead. |
| SharedMemory result transfer | Return buffer-typed slabs through shared memory rather than pickling. |
| Blocking / work-stealing scheduler | Only if a future recognized shape is not level-decomposable, which would mean breaking the §5.5.3 monotonicity constraint. |
| Vectorization advice in `explain` | Out of scope for a parallelizer (§16.4). |

The experimental schedulers `early_exit` and `branch_sensitive_deps` are
named by `activate(experimental=[...])`; a block that needs one runs
sequentially if the flag is off.

---

## 13. Confirmed Defaults

| Setting | Default |
|---|---|
| Pragma case sensitivity | Case-sensitive; `LUCEN`, uppercase |
| File scope | All files under `[scope]`, auto-included |
| Pool sizing | `os.cpu_count()` |
| Chunk count | Auto per backend (§5.7.1); `chunks=` overrides |
| Calibration | auto: static pre-screen, first-chunk or twin probe, memo |
| `reduction_order=` | `sequential_equivalent` |
| `[errors].mode` | `report` |
| Recognized-DAG wavefront | Sequential-by-default; parallel via explicit backend |

---

## 14. Core Design Invariant: Naive and Expert Parity

Every clause produces identical output to the naive path, with exactly two
deliberate exceptions: `reduction_order=stable|custom` (a different, declared
fold order) and `on_error=collect|custom` (a different, declared error
disposition). Every other clause, `calibrate=`, `chunks=`, `grainsize=`,
`backend=`, `trust=`, and the rest, trades time, observability, or a proof for
an assertion, never a different result. Codegen canon rule 6 extends the
invariant to performance: the naive zero-clause path is also the fastest
generated code.

---

## 15. The Comment Invariant

The pragmas are ordinary comments. A file with Lucen removed, uninstalled,
deactivated, or never present runs identically to one where Lucen never
existed. `ClauseValueError` can fire only while the Scanner is actively
parsing a marked file under an active hook, never at runtime in a program that
does not activate Lucen. The bytes prefilter (§5.1) means a pragma-free
file is not merely semantically untouched but almost entirely unexamined. The
worst case of adopting Lucen is the program you already had.

---

## 16. Performance Model and Budgets

### 16.1 Overhead inventory

| Scope | Cost |
|---|---|
| Per process | Pool creation (once, lazy); hook install. Amortized. |
| Per import (pragma-free file) | One bytes scan. |
| Per import (pragma file, cold) | Full pipeline; cached thereafter. |
| Per block call | Gate checks (microsecond-scale); probe (real work); memo lookup. |
| Per chunk | Dispatch; serialization (PROCESS only, pipelined, single-pickle); commit (slice-assign); audit (native, tier A O(1)). |
| Per iteration (zero-clause tier-A) | Zero added interception. |

### 16.2 Budgets

Provisional targets, CI-gated where a benchmark exists. Changing a budget is a
spec amendment, not a test edit.

| Budget | Target |
|---|---|
| Zero-clause chunk function vs hand-written loop | <= 1.05x |
| Import overhead, pragma-free file | <= 2% |
| Probe overhead (pure map) | ~0 by construction (§5.17) |
| End-to-end speedup, CPU-bound map, 12 cores | 3x to 4.3x on PROCESS |

Measured numbers across seven interpreters live in
[`BENCHMARK.md`](https://github.com/fcmv/lucen/blob/main/BENCHMARK.md).

### 16.3 The named ceiling: free-threaded shared-object contention

Stated as a design input. On free-threaded builds, sharing objects across
threads still scales sub-linearly: reading a shared container contends on its
refcount, and writing a shared list serializes on the list's per-object
mutation lock, even without a GIL. Lucen's posture:

- **Mitigate structurally:** the codegen canon (§5.16) hoists shared-object
  access to once per chunk, keeping cross-thread traffic off the per-iteration
  path.
- **Route around it:** §5.6.1 sends maps and reductions to PROCESS, which has
  no shared-object contention, on both builds.
- **Measure, do not promise:** `profile`'s per-block efficiency figure surfaces
  the ceiling empirically.
- **What Lucen cannot fix:** user objects genuinely hot across threads.
  That is CPython's frontier, named here so nobody discovers it as a Lucen
  bug.

### 16.4 Positioning honesty

Lucen accelerates Python-level loop bodies of meaningful size. It does not
compete with vectorized native kernels, and no output implies otherwise. The
profitability gate is the mechanical expression of this honesty: when parallel
dispatch cannot win, Lucen says so and stays sequential.

---

## 17. Invariant Audit

| Invariant | How the design upholds it |
|---|---|
| **Never an incorrect result** | Proven shapes: correctness by construction (disjoint slabs, ordered commit) plus tier-A/B audit. Asserted shapes: tier-B/C audit into the same conflict and re-run. Wavefront: level-decomposition proof (§5.8). Reductions: chunk-order fold (§5.12), native fold by reference (§5.7.5). Helpers: purity proof downgrades a proven-stateful one (§5.4). Serialization: pickle convergence rejects a value-shifting object (§5.13). |
| **Never disruptive** | Quiet fallback default; every downgrade reported, not raised. Recursion-headroom and spawn-safety guards convert crashes into sequential runs. `ClauseValueError`'s loudness is confined to Scanner-active parsing. |
| **Comment Invariant** | Pragmas are comments; the prefilter makes a pragma-free file nearly unexamined. |
| **Naive and expert parity** | Two declared exceptions, none added. Canon rule 6 extends it to generated-code cost. |
| **Reductions bit-identical** | Per-element contributions folded in sequential order, on the Python and the native path alike. |
| **Optional native core** | Every native operation has a pure-Python twin; the suite passes on both paths. |
| **Deadlock-freedom (recognized DAG)** | No task ever waits on another task; the only synchronization is the level barrier. |
| **Container state after mid-block error** | Exactly the sequential prefix on the slab path; the direct-write fast path documents its narrower guarantee (§5.7.4). |

---

*This document describes the shipped system. Where it and the code disagree,
that is a bug in one of them; file it against whichever is wrong.*
