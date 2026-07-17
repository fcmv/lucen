# 0013. Backend routing is interpreter-independent

## Context

Free-threaded CPython removes the GIL, so the intuitive expectation is that the
thread backend becomes the fast path there: threads share memory, avoid pickling,
and now run truly concurrently. An earlier design routed compute to threads on a
free-threaded build and to processes on a GIL build, choosing the backend by
interpreter.

Measurement on a real free-threaded build contradicted the intuition. For the
shapes Lucen parallelizes, threads are catastrophic: reading a shared
container contends on its reference count, and writing a shared list serializes
on that list's per-object mutation lock. On a shared-container workload the
thread backend measured far below sequential even at high thread counts. The
process backend has neither problem, because each worker owns its data.

## Decision

Route by the block's data shape, not by the interpreter. Maps and reductions
run on the process backend on both GIL and free-threaded builds. The cost model
costs the backend that will actually run, not the interpreter it runs on.

The thread backend is reserved for the cases where a process copy would lose the
result or cost more than it saves: a block whose only effect is in-place
mutation or a side effect through a reference (a process copy would drop it), and
an un-sliceable structured read (a process copy would ship the whole container to
every chunk). It is also always reachable through an explicit `backend=thread`.
On a free-threaded build only, a block whose measured per-iteration cost clears a
conservative floor is promoted from process to thread, because once the body is
heavy enough the contention is amortized and threads win by not marshalling.

## Consequences

- The fast path is the same on a laptop with the GIL and on a free-threaded
  server: correct and fast without the user knowing which interpreter they are
  on.
- The published free-threaded scaling ceiling (shared-object contention) is a
  design input, not a surprise. Lucen routes around it rather than promising
  to fix it.
- The recognized-DAG wavefront runs sequential-by-default on both builds; its
  parallel form is reached by an explicit `backend=thread` on a free-threaded
  build, because per-level process dispatch loses on a GIL build.

Spec: technical specification sections 5.6, 5.6.1, 16.3.
