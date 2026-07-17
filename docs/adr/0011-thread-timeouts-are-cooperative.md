# 0011. Thread timeouts are cooperative

## Context

The `timeout=` clause bounds a block's wall time. On the process backend a
worker is a separate process that can be hard-killed when it exceeds the
deadline. On the thread backend there is no equivalent: CPython threads cannot
be preempted or killed from outside. A thread running a tight loop or a blocking
C call will not stop just because a deadline passed.

A design that promised a hard per-iteration timeout on the thread backend would
be promising something the runtime cannot deliver.

## Decision

The timeout semantics differ by backend, honestly:

- **Process backend:** a hard kill at the deadline, with the documented cost
  that killing a worker recycles the pool (the surviving workers respawn), which
  is reported through the fallback channel.
- **Thread backend:** a cooperative deadline checked between iterations, and
  only when the `timeout=` clause is present so unbounded blocks pay nothing. A
  single iteration that never returns (a stuck C call, an infinite inner loop)
  cannot be preempted, and this limitation is documented rather than hidden.

## Consequences

- The timeout does what the backend can actually enforce, and the thread
  backend's limitation is stated where the user configures the clause.
- A block that needs a genuinely hard timeout on an individual iteration must
  run on the process backend, where a worker can be killed.
- The cooperative check is clause-conditional, so a block without `timeout=`
  has no per-iteration deadline read, consistent with the codegen rule that
  instrumentation is emitted only when requested.

Spec: technical specification section 7.
