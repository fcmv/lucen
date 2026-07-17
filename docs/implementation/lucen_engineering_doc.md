# Lucen Engineering Guide

**Companion:** [`lucen_technical_spec.md`](../spec/lucen_technical_spec.md), the decision record. Where this guide and the spec disagree, the spec wins and this guide has a bug.

This document maps the shipped code: where each concern lives, how a marked
block flows from source text to parallel execution, what the native core does,
and how the invariants are held in tests. It is the orientation a contributor
reads before touching the pipeline. A `[§x.y]` reference points at the spec.

---

## 1. Repository Layout

```
lucen/                        # the pure-Python package; the whole implementation
  __init__.py                    # activate() / deactivate() / get_fallback_report() [§5.1]
  import_hook.py                 # the scoped import hook and rewrite cache [§5.1]
  codegen/                       # chunk function + sequential twin emission [§5.16]
    artifact.py                  # SlabPlan, ReductionPlan, ChunkArtifact
    generator.py                 # generate, the AST transform, and assembly
  analysis/                      # the compile-time analysis pipeline
    scanner.py                   # bytes prefilter + tokenize-based pragma scan [§5.1, §5.2]
    rewriter.py                  # variable classification, audit-tier tag [§5.3, §5.4]
    analyzer.py                  # dependency-shape recognition [§5.5]
    purity.py                    # callable purity proof [§5.4]
    selector.py                  # eligibility ladder + routing [§5.6]
    trust.py                     # trusted-callable resolution [§5.3.4]
  execution/                     # the runtime: routing, dispatch, commit
    dispatch.py                  # calibration, backend choice, THREAD driver, commit orchestration
    planning.py                  # iteration-space plan and chunk bounds [§5.7.1]
    affinity.py                  # CPU affinity [§7]
    process_backend.py           # persistent pool, slicing, single-pickle, rehydration [§5.10]
    wavefront.py                 # level-synchronous DAG scheduler [§5.8]
    early_exit.py                # experimental break scheduler [§5.9.1]
    runtime.py                   # slab commit, reduction fold, SKIP sentinel [§5.7.2, §5.12]
    preflight.py                 # the gate: purity, convergence, spawn scan, headroom [§5.13]
    nested_guard.py              # nested-region guard [§5.11]
    _accel.py                    # native seam: native op or pure-Python twin [§5.7.5]
  support/                       # shared support
    config.py                    # lucen.toml load + precedence + validation [§8]
    errors.py                    # three-axis error model, fallback report [§5.14]
    costmodel.py                 # static profitability pre-screen [§5.17]
    cache.py                     # on-disk rewrite cache
  clauses/registry.py            # clause grammar and validation [§7]
  cli/                           # explain / profile [§5.15]
lucen_core/                   # the optional Rust crate, imported as lucen._core
  src/lib.rs                     # audit_index_bitmap, fold_ordered, audit_contiguous (+ cargo tests)
tests/
  unit/ integration/             # the invariant suite
  property/ formal/              # differential fuzzing and model-checked specs
  benchmarks/                    # bench.py, matrix_bench.py, routing_check.py, bench_vs_hand.py
docs/
  spec/ implementation/ adr/     # this guide, the spec, decision records
  formal/ paper/                 # model checks and the preprint
```

The crate is small on purpose. Correctness is upheld by construction
(privatized slabs, ordered commit, level scheduling), not by per-element
native machinery, so the native core holds only what is genuinely hot and has
an exact native equivalent: the write-set audit and the ordered reduction fold
[§5.7.5]. Everything else is Python.

---

## 2. The Pipeline

A marked block passes through two stages: an import-time compile and a
call-time execution. The import-time stages are pure functions of the source
text and are cached; the call-time stages depend on runtime data and the live
interpreter.

### Import time

```
source bytes
  -> scanner.scan_source        prefilter, then tokenize; ClauseValueError on a bad clause
  -> rewriter.analyze_source    classify every name; tag the audit tier
  -> analyzer                   recognize the dependency shape
  -> selector.select            eligibility + backend routing + static pre-screen
  -> codegen.generate           emit the chunk function and the sequential twin
  -> import hook                cache the rewritten module, keyed by content + version
```

`scan_source` is the only stage that touches raw bytes. The prefilter makes a
pragma-free file skip everything else [§5.1]. `analyze_source` returns one
`BlockAnalysis` per marked block; `select` turns it into a `BlockDecision`;
`generate` turns the pair into a `ChunkArtifact` carrying both generated
functions and the slab and reduction plans. A block the selector routes to
`SEQUENTIAL` produces no artifact and runs as plain Python.

### Call time

```
dispatch.execute
  -> preflight gate             purity, recursion headroom, spawn scan, pickle convergence [§5.13]
  -> calibration probe          chunk 0 measured; twin-probe for pure maps [§5.17]
  -> backend choice             interpreter-independent routing [§5.6.1]
  -> flat dispatch or wavefront THREAD (dispatch.py) or PROCESS (process_backend.py) or the level driver [§5.8]
  -> join + write-set audit     one native call when present [§5.7.3]
  -> commit in chunk order      slab commit or direct sub-range write [§5.7.2, §5.7.4]
  -> reduction fold             per-element, sequential order, native by reference [§5.12]
```

Every downgrade at any call-time stage routes into the same place: the block
runs on its sequential twin and the reason is recorded in the fallback report
[§5.14]. Nothing commits before the join, so a discard is free.

---

## 3. Where Each Invariant Is Enforced

- **Never an incorrect result** lives in three places. Proven-disjoint writes
  are correct by construction in `codegen.py` and `runtime.py` (private slab,
  chunk-ordered commit). Asserted-disjoint writes are checked at the join by
  the write-set audit in `_accel.py` / `runtime.py` [§5.7.3]. Stateful helpers
  are caught before dispatch by `purity.py` through `preflight.py` [§5.4], and
  value-shifting serialization by the convergence check in `preflight.py`
  [§5.13].
- **Never disruptive** lives in `errors.py` (quiet fallback default, once-per-
  block logging) and in the `preflight.py` guards that turn a would-be crash
  (recursion exhaustion, unguarded spawn) into a sequential run.
- **The Comment Invariant** lives in `scanner.py`'s prefilter and in the
  import hook: a file without the token is loaded unchanged [§5.1, §15].
- **Reductions bit-identical** lives in `runtime.fold_contributions` and its
  native counterpart `fold_ordered`: per-element contributions folded in
  sequential order, never a re-associated partial-sum tree [§5.12].
- **Naive and expert parity** lives in `codegen.py` canon rule 6: instrumentation
  is emitted only when a clause asks for it, so the zero-clause path is both
  the default and the fastest generated code [§5.16, §14].

---

## 4. The Native Seam

`lucen._accel` is the single boundary between the pure-Python
implementation and the optional Rust core. It imports `lucen._core` when
present and falls back to pure Python otherwise, so the same source runs
correctly on any interpreter and under `LUCEN_DISABLE_NATIVE=1`.

Two orchestration loops cross into the core, each because it is hot at Python
level and has an exact native equivalent:

- **`audit_index_bitmap`** runs the whole tier-B/C write-set audit over every
  chunk's key list in one native call, a word-wise bitmap OR, rather than a
  Python driver paying a native method call per index.
- **`fold_ordered`** folds reduction contributions *by reference* through
  CPython's own number protocol and rich comparison, in the exact order the
  sequential loop uses. Because it calls the same C-level operation the
  interpreter calls for each operator, the result matches sequential for every
  operand type: float bits, unbounded integers without wrapping, user-defined
  operators. The not-handled signal is a sentinel, not `None`, so a user
  operator that legally returns `None` never triggers a second fold.

Two things were built, measured, and kept in Python, with the measurements
recorded in the code so nobody re-attempts them blind: the element-wise slab
commit (slower than CPython's `zip` plus specialized list stores) and a
data-marshalling f64 fold (retired in favor of the by-reference fold). The
rule for adding anything to the core: it must be measured faster than the
Python twin on representative data, and it must pass the parity tests that
prove it returns exactly what the twin returns.

Native loop-body compilation is roadmap, not this seam [§12]. The core moves
the orchestration, not the loop body.

---

## 5. Backend Routing

`dispatch._pick_backend` decides the backend from the block's data shape, not
the interpreter [§5.6.1]. The rules, in order:

1. A block with no output to ship back (in-place mutation, or a side effect
   through a reference) goes to THREAD: a process copy would drop the effect.
2. A structured read that cannot be sliced per chunk goes to THREAD: PROCESS
   would ship the whole container to every chunk.
3. Everything else (maps, reductions, sliceable structured reads) goes to
   PROCESS on both GIL and free-threaded builds.
4. An explicit `backend=` is honored as written.
5. On a free-threaded build only, a block whose measured per-iteration cost
   clears a floor is promoted PROCESS to THREAD.

`tests/benchmarks/routing_check.py` is the guard: it times every workload on
every backend and asserts the gate picks the fastest, and that every backend
is bit-identical to plain Python. A routing change that does not keep both
properties is a regression.

---

## 6. Testing

The suite is the specification made executable. The load-bearing categories:

- **Cross-backend equivalence** (`tests/integration`): every workload
  bit-identical across sequential, thread, and process, and against the
  pragma-as-comment baseline.
- **Native parity** (`tests/unit/test_accel.py`): every native operation
  returns exactly what its pure-Python twin returns. Bignum folds do not wrap,
  float folds are `repr`-equal, min/max keeps the incumbent on a tie, SKIP gaps
  are skipped, multi-site fold order is sequential, a hostile `__setitem__` is
  called exactly once per real store. The whole suite is re-run under
  `LUCEN_DISABLE_NATIVE=1` in CI so the fallback path is never allowed to rot.
- **Purity and trust** (`tests/unit/test_purity.py`): a proven-stateful helper
  runs sequentially and correctly; a pure helper keeps its parallel routing;
  every trust override restores parallel routing.
- **Error post-state and dict order**: a mid-block error leaves the container
  exactly at the sequential prefix; chunk-ordered merge reproduces sequential
  insertion order; a duplicate key triggers the tier-B audit and the re-run
  matches sequential.
- **Adversarial corpus** (`lucen_trial/`): the black-box red-team scenarios.
  Everything either runs bit-identically or refuses loudly.

Run `pip install -e ".[dev]" && pytest`. The full suite is a few seconds.
`LUCEN_DISABLE_NATIVE=1 pytest tests/unit` runs the fallback path.

---

## 7. Contributing to the Pipeline

The bar for a change touching the execution pipeline is the invariant suite:
correctness is proven bit-identical across backends, and no routing change
lands without benchmark evidence from `routing_check.py`.

Two checkpoints deserve explicit attention in review:

- **The dependency-shape vocabulary.** A change to `analyzer.py`'s
  shape-normalization must preserve level-decomposability for any DAG form:
  every dependency index provably at or below `i // c` for constant `c > 1`
  [§5.5.3, §5.8]. This is what lets the wavefront exist without a blocking
  scheduler.
- **The codegen hot path.** A change to `codegen.py` or any per-iteration path
  must not add interception to the zero-clause tier-A block [§5.16 rule 6]. The
  zero-interception test asserts this on the generated code, not just on timing.

The decision records under `docs/adr/` capture why specific non-obvious choices
were made, so a future change that proposes to "simplify" one of them finds the
reasoning before re-introducing a bug the design already fixed once.

---

*This guide describes the shipped system. For the authoritative semantics, read
the spec.*
