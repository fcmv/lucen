# AI-Assisted Contributions

This document is the project's policy on contributions produced with the help
of AI tools: large language models, code assistants, agentic coding tools, and
anything similar. It is a contribution policy, a companion to
[CONTRIBUTING.md](CONTRIBUTING.md), not a disclosure form.

The short version: **we do not care whether you used an AI. We care that you
stand behind what you submit, understand it, and that it meets the same bar as
any other contribution.** Lucen is a correctness-first parallelizing
compiler, and that bar is high for a reason.

## The principle

A contribution is judged on what it is, not on how it was produced. If you used
an AI tool to help write code, tests, or documentation, that is fine, and it
changes nothing about the standard your contribution is held to. The tool is
yours; the contribution is yours; the responsibility is yours.

This cuts both ways. It means AI assistance is welcome and does not need to be
disclosed or apologized for. It also means AI assistance is never an excuse. "A
tool generated it" does not lower the quality bar, shorten the review, or shift
responsibility for a defect.

## What you are responsible for

When you open a pull request or an issue, you are asserting the following, and
they hold whether you wrote every character yourself or a tool wrote most of it:

1. **You understand it.** You can explain what the contribution does, why it is
   correct, and how it fits the codebase, in your own words, in review. If you
   cannot explain a piece of your contribution, it is not ready to submit.
   Submitting code you do not understand is the one thing this policy exists to
   prevent.

2. **You have verified it.** For Lucen specifically, this means the
   contribution passes the correctness bar in [CONTRIBUTING.md](CONTRIBUTING.md):
   the bit-identical invariant suite on both the native and the pure-Python
   fallback paths, and, for a routing change, the routing benchmark. AI tools
   reason confidently about parallel correctness and are frequently wrong about
   it. The tests, not the tool's explanation, are the evidence. Do not trust a
   model's claim that a parallel transformation is safe; prove it with the
   suite.

3. **You have the right to contribute it.** You affirm that the contribution is
   yours to license under Apache-2.0 (see [CONTRIBUTING.md](CONTRIBUTING.md) and
   [LICENSE](LICENSE)). An AI tool can reproduce code from its training data,
   including code under incompatible licenses. You are responsible for ensuring
   your contribution is not an unattributed copy of someone else's work. If a
   tool produced something that looks like it may have been memorized verbatim
   from a specific source, do not submit it.

4. **It is a genuine contribution.** It solves a real problem, it is scoped to
   review, and it is something you would be comfortable putting your name on
   without the tool in the room.

## What is not welcome

The failure mode this policy guards against is the cost asymmetry AI creates: it
is now cheap to generate a large volume of plausible-looking code, tests, or
issue reports, and expensive for a human maintainer to review them. Do not shift
that cost onto the project.

Specifically, the following are not welcome, regardless of how they were
produced:

- **Low-effort generated pull requests** that the author has not read,
  understood, and verified. A pull request full of AI-generated code the author
  cannot explain will be closed.
- **Automated or bulk issue reports** produced by running a tool over the
  codebase and pasting the output, without a human confirming the finding is
  real, reproducible, and correctly described. A "your code has these problems"
  dump is not a contribution.
- **Speculative or hallucinated bug reports**, including reports of behavior the
  tool inferred from reading code rather than from actually running Lucen.
  For a correctness issue, a runnable reproduction that shows the sequential and
  parallel results is required (see [SECURITY.md](SECURITY.md) for the wrong
  result case).
- **Documentation or comments that describe the code as the tool imagines it**
  rather than as it is. Lucen's documentation is held to the same accuracy
  standard as its code.

## Disclosure

You are not required to disclose that you used an AI tool, and using one is not
held against you. What is required is that you do not misrepresent: do not claim
to have manually verified something a tool asserted, and do not present
generated output as more tested or more understood than it is. Honesty about
what you have actually checked is the same expectation applied to every
contributor.

## Why this policy exists

Lucen makes a promise its users rely on: a parallel run is bit-identical to
plain sequential Python. Holding that promise depends on every change being
understood and verified by a person who takes responsibility for it. AI tools
are good at producing code that looks correct and are unreliable at reasoning
about the exact concurrency and ordering properties Lucen must preserve. This
policy keeps the human accountability that the guarantee depends on, while
leaving contributors free to use whatever tools help them do good work.
