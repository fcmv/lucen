# AI-Assisted Contributions

The policy on contributions produced with help from AI tools: language models,
code assistants, agentic coding tools, and anything similar. A companion to
[CONTRIBUTING.md](CONTRIBUTING.md), not a disclosure form.

A contribution is judged on what it is, not on how it was produced. Using an AI
tool is fine and does not need to be disclosed. It is also never an excuse: "a
tool generated it" does not lower the bar, shorten the review, or shift
responsibility for a defect.

## What you are asserting

Opening a pull request or an issue asserts all of the following, whether you
wrote every character or a tool wrote most of them.

1. **You understand it.** You can explain in review, in your own words, what the
   contribution does and why it is correct. If you cannot explain a piece of it,
   it is not ready to submit.

2. **You have verified it.** The bit-identical invariant suite passes on both
   the native and the pure-Python fallback paths, and a routing change carries
   the routing benchmark; [CONTRIBUTING.md](CONTRIBUTING.md) has the commands.
   Models reason confidently about parallel correctness and are frequently
   wrong about it, so the evidence is the suite, not the tool's explanation.

3. **You have the right to contribute it.** The contribution is yours to license
   under Apache-2.0 (see [LICENSE](LICENSE)). A model can reproduce code from
   its training data, including code under incompatible licenses; if something
   looks memorized verbatim from a specific source, do not submit it.

4. **It is a genuine contribution**, solving a real problem and scoped so that a
   reviewer can hold it in their head.

## What is not welcome

AI has made it cheap to generate plausible-looking code, tests and issue
reports, and left the cost of reviewing them where it was. Do not shift that
cost onto the project.

- **Pull requests the author has not read, understood and verified.** One full
  of generated code the author cannot explain will be closed.
- **Bulk issue reports** from running a tool over the codebase and pasting the
  output, without a human confirming each finding is real, reproducible, and
  described correctly.
- **Reports of behavior inferred from reading the code** rather than from
  running Lucen. A correctness issue needs a runnable reproduction showing the
  sequential and the parallel result; [SECURITY.md](SECURITY.md) covers the
  wrong-result case.
- **Documentation or comments that describe the code as the tool imagines it.**
  The documentation is held to the same accuracy standard as the code.

## Disclosure

Disclosure is not required and using a tool is not held against you.
Misrepresentation is the problem: do not claim to have manually verified
something a tool asserted, and do not present generated output as more tested
than it is.
