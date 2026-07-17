# Governance

This document describes how decisions are made in Lucen: who makes them, how,
and what is and is not open to change. It is deliberately simple, matching the
project's current size, and it is written to grow as the project does.

## Principles

Lucen is governed by two ideas that sit above any process described here.

**The invariant is constitutional.** Lucen guarantees that a parallel run is
bit-identical to the same file executed as plain sequential Python, with no tier
and no opt-out. This guarantee is not a technical preference to be traded off
against performance or convenience by a vote or a maintainer's discretion. It is
the foundation the project exists to provide. A proposal that would relax it is
out of order, no matter who makes it or how it is argued. Every other decision
is made in service of it.

**Decisions are made in the open, with reasons.** Technical decisions are made
in public issues and pull requests, with the reasoning recorded, so that a
future contributor can understand why a choice was made rather than
rediscovering it. Non-obvious decisions are captured as architecture decision
records under `docs/adr/` so they are not re-litigated by accident.

## Roles

### Users

Anyone who uses Lucen. Users shape the project by reporting bugs, asking
questions, and describing real workloads. A well-documented bug report,
especially one that shows a divergence from sequential execution, is one of the
most valuable contributions to a correctness-first project.

### Contributors

Anyone who submits a change: code, tests, documentation, benchmarks, or
triage. Contributors work through the process in
[CONTRIBUTING.md](CONTRIBUTING.md). There is no formal membership step to become
a contributor; opening a well-formed pull request or issue is enough.

### Maintainers

Contributors who have earned the responsibility of reviewing and merging
changes, cutting releases, and stewarding the project's direction. Maintainers
hold the invariant on behalf of users. A maintainer's core job is not to write
the most code; it is to ensure that everything merged upholds the guarantees,
and to say no, with a reason, when a change would not.

Maintainers are listed in the repository (for example in a `CODEOWNERS` file or
the release metadata) as the project grows. While the project is small, it is
maintainer-led by its founding maintainer, and this document describes the model
the project is committed to as more maintainers join.

## Decision-making

Most decisions never need a formal process. The great majority of changes are
proposed as pull requests, reviewed against the bar in
[CONTRIBUTING.md](CONTRIBUTING.md), and merged when a maintainer approves them.

For decisions that are larger, contested, or that set precedent, the project
uses **lazy consensus with a maintainer as the tie-breaker**:

1. A proposal is made in a public issue, with enough detail to evaluate it.
2. Contributors and maintainers discuss it. Silence is assent: if no one with a
   stake objects within a reasonable time, the proposal carries.
3. If there is disagreement, the discussion works toward a resolution on the
   technical merits, measured against the invariant and against the evidence
   (benchmarks for performance claims, the equivalence suite for correctness
   claims).
4. If consensus cannot be reached, a maintainer makes the call and records the
   reasoning. A decision that touches the invariant is decided in the invariant's
   favor by default; the burden is on any proposal that would weaken it, and
   that burden cannot be met by convenience or speed alone.

Evidence outranks opinion. A claim that a change is faster is settled by a
benchmark, not by argument. A claim that a change is still correct is settled by
the cross-backend equivalence suite, not by reasoning about it. This is a
deliberate cultural choice: it keeps the project's decisions grounded in the
same standard it holds its code to.

## Becoming a maintainer

Maintainership is offered to contributors who have demonstrated, over a series
of contributions, sound judgment about the project's guarantees and care for the
codebase. The relevant signals are:

- A track record of changes that hold the correctness bar, including the tests
  and benchmarks that prove it.
- Reviews that catch real problems, especially ones that would have weakened an
  invariant.
- Reliability and good faith in discussion, consistent with
  [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

An existing maintainer proposes a contributor for maintainership; the existing
maintainers decide by consensus. The bar is trust with the invariant, not volume
of code.

## Changing this document

This governance model is expected to evolve as the project grows, for example
toward a documented maintainer roster and a more formal steering process once
there are several maintainers. Changes to this document follow the same
decision-making process described above, with one exception that is not subject
to process: the constitutional status of the correctness invariant is not
amendable through ordinary governance. A project that traded that guarantee away
would no longer be Lucen.

## Reporting conduct issues

Code of Conduct concerns are handled per [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
Security issues, including any suspected silent-wrong-result vulnerability, are
handled privately per [SECURITY.md](SECURITY.md).
