# Security Policy

Lucen rewrites and parallelizes code that its users trust to produce
correct results. That places it in an unusual security position for a
library: its most serious failure is not a crash or a leak, it is a *silent
incorrect result*, a program that runs to completion and returns the wrong
answer without any indication that anything went wrong. This policy is written
around that reality.

## Supported versions

Security fixes are applied to the latest released minor version. Because the
project follows semantic versioning, a fix that does not change public
behavior ships as a patch release; a fix that must change behavior to be
correct ships as documented in the release notes.

| Version | Supported |
|---|---|
| Latest minor release | Yes |
| Older releases | Best effort; upgrade recommended |

## The threat model

Lucen has one guarantee with no tier and no opt-out: a parallel run is
bit-identical to the same file executed as plain sequential Python. Security
severity here is measured against that guarantee, not against a generic
crash-and-leak taxonomy. In descending order of severity:

### Critical: a silent incorrect result inside the contract

A marked block that produces a result different from its sequential execution,
without a fallback report, without an exception, and without the user having
made one of the documented trust waivers. This is the highest-severity class
of issue in Lucen. It is a direct violation of the core guarantee, and it
is treated with more urgency than a crash, because a crash is visible and a
wrong answer is not.

The documented trust boundary is the explicit exception (see
[LIMITATIONS.md](LIMITATIONS.md), section 1): a divergence that requires a
stateful helper the analyzer cannot read, a serializer that does not preserve
value, or the two deliberate assertions `depend=none` plus
`skip_runtime_check=true`, is a documented limitation, not a vulnerability.
A divergence that requires *none* of those, that is, one reachable from
ordinary correct-looking code, is a critical security issue.

### High: a crash or hang caused by Lucen on code that runs fine without it

A marked file that Lucen causes to raise, deadlock, or exhaust resources
where the same file with the pragmas treated as comments runs cleanly. This
violates the never-disruptive guarantee. The intended behavior for anything
Lucen cannot handle is a quiet sequential fallback, so a crash or hang
attributable to Lucen is a defect of the same family, one severity below a
silent wrong result because it is at least visible.

### Moderate: unexpected code execution surface

Lucen generates and compiles code from marked source and, on the process
backend, ships pickled payloads to workers. Any path by which Lucen would
execute or deserialize something the user did not put in their own source, or
would widen the deserialization surface beyond what `multiprocessing` already
implies for the user's own objects, is in scope. Lucen does not fetch,
download, or execute remote code, and it introduces no network surface; a
report showing otherwise is a moderate-or-higher issue depending on impact.

### Informational: documented limitations

The items in [LIMITATIONS.md](LIMITATIONS.md) are not vulnerabilities. If you
believe a documented limitation is more severe or more easily reached than the
document states, that itself is worth reporting, and it will be evaluated
against the categories above.

## Reporting a vulnerability

Please report suspected security issues **privately**, not in a public issue,
so that a fix can be prepared before the details are public.

- Use GitHub's private vulnerability reporting for this repository
  (the "Report a vulnerability" button under the Security tab), which opens a
  private advisory visible only to you and the maintainers.

When you report, include as much of the following as you can. A minimal
reproduction is worth more than a long description:

- A minimal marked source file that triggers the issue.
- The interpreter and platform (for example, CPython 3.12 on Windows, GIL
  build), since routing and the native core differ across these.
- What the sequential result is (the same file with Lucen not activated)
  and what the parallel result is, so the divergence is concrete.
- Whether the native extension is present or the pure-Python fallback is in
  use (`LUCEN_DISABLE_NATIVE=1` forces the fallback), since a divergence on
  only one path localizes the bug immediately.

A silent-wrong-result report is most useful when it shows the two outputs side
by side and names which trust waivers, if any, were in effect. If none were,
say so explicitly; that is the line between a critical issue and a documented
limitation.

## What to expect

- **Acknowledgement** that the report was received and is being investigated.
- **An initial assessment** placing the report in one of the categories above,
  with the reasoning, so you can push back if you think it is under-rated.
- **A fix or a decision.** A confirmed critical or high issue is prioritized
  above feature work. A report that resolves to a documented limitation is
  explained against this policy and [LIMITATIONS.md](LIMITATIONS.md).
- **Coordinated disclosure.** Once a fix is available, the advisory is
  published with credit to the reporter unless you ask to remain anonymous.

## Scope

In scope: anything in the `lucen` Python package, the `lucen_core` native
crate, the generated code, the process-backend serialization path, and the
CLI. Out of scope: vulnerabilities in Python itself, in the standard library,
in third-party packages a user's loop body happens to call, and behaviors
explicitly documented as the trust contract in
[LIMITATIONS.md](LIMITATIONS.md).
