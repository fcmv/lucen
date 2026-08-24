# Governance

How decisions are made in Lucen: who makes them, and what is not open to
change. The model is simple because the project is small, and is expected to
grow with it.

## Principles

**The correctness invariant is not up for a vote.** A parallel run is
bit-identical to the same file executed as plain sequential Python, with no tier
and no opt-out. That is not a preference to be traded against performance or
convenience by a vote or a maintainer's discretion, and a proposal to relax it
is out of order regardless of who makes it.

**Decisions are made in the open, with reasons.** Technical decisions happen in
public issues and pull requests with the reasoning recorded, and non-obvious
ones are captured as architecture decision records under `docs/adr/` so they are
not re-litigated by accident.

## Roles

**Users** shape the project by reporting bugs, asking questions, and describing
real workloads. A bug report showing a divergence from sequential execution is
the most valuable kind here.

**Contributors** submit changes: code, tests, documentation, benchmarks, or
triage, through the process in [CONTRIBUTING.md](CONTRIBUTING.md). There is no
membership step; a well-formed pull request or issue is enough.

**Maintainers** review and merge changes, cut releases, and steward direction.
The job is to ensure everything merged upholds the guarantees and to say no,
with a reason, when a change would not.

The project is currently maintainer-led by its founding maintainer. Maintainers
will be listed in the repository, in `CODEOWNERS` or the release metadata, as
more join.

## Decision-making

Most changes need no formal process: propose a pull request, have it reviewed
against the bar in [CONTRIBUTING.md](CONTRIBUTING.md), and a maintainer merges
it.

Larger, contested, or precedent-setting decisions use **lazy consensus with a
maintainer as the tie-breaker**:

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

Evidence outranks opinion throughout: a claim that a change is faster is settled
by a benchmark, and a claim that it is still correct by the cross-backend
equivalence suite.

## Becoming a maintainer

Maintainership is offered to contributors who have shown, over a series of
contributions, sound judgment about the project's guarantees. The signals are:

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

This model is expected to evolve, toward a documented maintainer roster and a
more formal steering process once there are several maintainers. Changes follow
the decision-making process above, with one exception: the correctness
invariant is not amendable through ordinary governance.

## Reporting conduct issues

Code of Conduct concerns are handled per [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
Security issues, including any suspected silent-wrong-result vulnerability, are
handled privately per [SECURITY.md](SECURITY.md).
