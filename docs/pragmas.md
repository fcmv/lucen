# Pragma and clause reference

Lucen is driven by three comment pragmas. This page is the complete
reference for them and for every clause they accept. The accepted forms here
are the ones the clause validator enforces; a malformed clause is a loud
import-time `ClauseValueError` with a did-you-mean suggestion, never a silent
ignore.

## The three pragmas

| Pragma | Placement | Purpose |
|---|---|---|
| `# LUCEN START [clauses]` | Immediately above a `for` loop | Opens a marked block and carries the tuning and assertion clauses |
| `# LUCEN END` | Immediately below the same loop | Closes the marked block |
| `# LUCEN TRUST [clauses]` | Immediately above a `def` | Asserts a helper is safe to run under parallelism |

The pragmas are ordinary comments. A file with Lucen not activated runs
exactly as if they were absent. Clauses are comma-separated on the pragma line,
for example:

```python
# LUCEN START calibrate=false, timeout=5.0, on_error=collect
```

Every clause only ever trades a compiler-held proof for a programmer-held
assertion, or exactness for speed. None of them changes the computed result;
the ones that assert something the runtime can check are still checked.

## Clauses on `# LUCEN START`

### Backend and scheduling

| Clause | Accepted forms | What it does |
|---|---|---|
| `backend` | `thread` \| `process` \| `sequential` \| `thread(pool_size=N, chunks=M)` \| `process(chunks=M, pool=<factory>)` | Pins the execution backend and its worker and chunk counts, overriding the automatic routing. |
| `calibrate` | `true` \| `false` \| `static` \| `always` \| `threshold(min_gain=<float>)` | Controls the profitability gate. `false` forces the parallel path; `threshold` sets the minimum projected gain. Every setting produces identical output. |
| `grainsize` | `<N>` \| `<N>(min_workers=M)` | Sets the level width for a recognized-DAG wavefront block. |
| `affinity` | `compact` \| `scatter` \| `explicit(cores=[...][, numa_node=N])` | Requests CPU affinity for the workers. |
| `nested` | `sequential` \| `shared_pool` \| `independent` | Policy for a marked block reached while another is already dispatching. |

### Correctness assertions (expert)

These waive a compiler proof. They are the only clauses that can affect
correctness, and only when the assertion is false; where the runtime can still
check the assertion, it does.

| Clause | Accepted forms | What it does |
|---|---|---|
| `depend` | `none` \| `acyclic(order=<callable>)` | Asserts iterations are independent (`none`), or that a dependency is acyclic under a given order. A false `depend=none` is still caught by the runtime write-set audit. |
| `skip_runtime_check` | `true` \| `false` | Disables the runtime write-set audit. Only meaningful together with `depend=none`; the two together are the only way to reach a silent wrong result, by design. |
| `trust` | `callables` \| `pickle` \| `all` | Waives the helper-purity proof (`callables`), the pickle-convergence check (`pickle`), or both (`all`). |

### Reductions

| Clause | Accepted forms | What it does |
|---|---|---|
| `reduce` | `sum` \| `prod` \| `min` \| `max` \| `count` \| `any` \| `all` \| `bit_and` \| `bit_or` \| `bit_xor` \| `concat` \| `custom(fn=<callable>, identity=<value>[, tree=false])` | Names the reduction operator, or supplies a custom associative one with its identity. |
| `reduction_order` | `sequential_equivalent` \| `stable` \| `custom(combine=<callable>)` | How partial results are combined. The default `sequential_equivalent` is bit-identical to sequential; `stable` permits a reproducible tree-combine. |

### Errors and timeouts

| Clause | Accepted forms | What it does |
|---|---|---|
| `timeout` | `<seconds>` \| `<seconds>(per_task=true[, on_timeout=<callable>])` | Bounds the block's wall time, raising `ParallelTimeoutError`. `per_task` applies the bound per iteration. |
| `on_error` | `collect` \| `collect(max_errors=N)` \| `custom(handler=<callable>)` | Gathers per-iteration exceptions instead of failing fast; readable afterward with `lucen.get_collected_errors`. |
| `strict` | `true` \| `false` \| `true(allow=[reason, ...])` | Turns this block's fallbacks into hard errors, optionally allowing named downgrade reasons. |
| `on_fallback` | `hard` \| `quiet` \| `report` \| `<mode>(allow=[reason, ...])` \| `custom(handler=<callable>)` | Sets how a fallback is surfaced for this block. |

### Observability

| Clause | Accepted forms | What it does |
|---|---|---|
| `progress` | `true` \| `false` \| `callback(<callable>[, per_task=true, include_result=true])` | Reports per-chunk or per-iteration progress. |

## Clauses on `# LUCEN TRUST`

`# LUCEN TRUST` above a `def` asserts that helper is safe under parallelism,
overriding the purity proof for it.

| Clause | Accepted forms | What it does |
|---|---|---|
| `args` | `checked` \| `unchecked` \| `unchecked(only=[name, ...][, skip_runtime_check=true])` | How the helper's arguments are treated: checked as reads, or trusted, optionally for named arguments only. |
| `qualname` | `Class.method` \| `Class.method(module=exact.path)` \| `<registry_key>` | Identifies the callable the trust applies to when the bare name is ambiguous. |

## Configuration file

Project-wide defaults and hard ceilings live in `lucen.toml` at the project
root: pool sizes, chunk counts, timeout ceilings, the error mode, an
experimental-features veto, and a `[trust] callables` list. Its precedence
runs from the built-in default, through `[defaults]`, through a per-block
pragma clause, clamped by `[limits]`. See the
[technical specification](spec/lucen_technical_spec.md) for the full schema.

## Removed clauses

Two earlier clauses were removed and now fail loud with a pointer to the
current form rather than being silently ignored: `process_wait` (recognized-DAG
blocks run on the wavefront driver automatically) and `batch_size` (renamed to
`chunks=`, a sub-argument of `backend=`).
