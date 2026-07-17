# Getting Support

This page is a map of where to go for each kind of question or report, so you
reach the right channel the first time.

## Start with the documentation

Most questions are answered by the docs, and they are written to be read:

- **[README.md](README.md)** has a complete tutorial from `pip install lucen`
  through marking a loop, activating, reading `explain` output, and
  understanding a refusal. If you are new, start there.
- **[LIMITATIONS.md](LIMITATIONS.md)** is the honest inventory of what Lucen
  does not do and where its guarantee's boundary lies. If Lucen is refusing
  to parallelize a block, or behaving differently than you expect, the answer is
  very often here.
- **[BENCHMARK.md](BENCHMARK.md)** has the measured performance across
  interpreters, so you can calibrate expectations against real numbers.
- The **[technical specification](docs/spec/lucen_technical_spec.md)** is the
  authority on every semantic, and the
  **[engineering guide](docs/implementation/lucen_engineering_doc.md)** maps
  the codebase.

The tools themselves are also built to answer questions. `lucen explain
yourfile.py` reports, statically, what Lucen decided for every marked block
and why. `lucen profile yourscript.py` reports what actually ran. If you are
wondering why a block did or did not parallelize, ask the tool first.

## Where to take each kind of thing

| You have | Go to |
|---|---|
| A usage question ("how do I...", "why does this block run sequentially?") | GitHub Discussions (or a `question` issue if Discussions is not enabled) |
| A reproducible bug, including any parallel result that differs from sequential outside the documented trust contract | A GitHub issue with a minimal reproduction |
| A suspected security issue, especially a silent wrong result reachable from ordinary code | The private channel in [SECURITY.md](SECURITY.md), not a public issue |
| A feature idea or a limitation you want closed | A GitHub issue; check [ROADMAP.md](ROADMAP.md) first to see if it is already planned |
| A change you want to contribute | [CONTRIBUTING.md](CONTRIBUTING.md) |

## Writing a good bug report

Lucen is a correctness-first parallelizing compiler, so the single most
useful bug report is one that makes a divergence concrete. When you can, include:

- A **minimal marked source file** that triggers the behavior.
- The **interpreter and platform** (for example, CPython 3.12 on Windows, GIL
  build). Routing and the native core differ across these, so this often
  localizes the issue immediately.
- For a wrong or unexpected result, **both outputs**: the file run with Lucen
  activated, and the same file with the pragmas treated as comments (Lucen
  not activated). That difference is the bug.
- Whether the **native core or the pure-Python fallback** was in use.
  `LUCEN_DISABLE_NATIVE=1` forces the fallback; a divergence on only one path
  tells the maintainers exactly where to look.

A reproduction that a maintainer can run and see fail is worth more than any
amount of description.

## What this project does not provide

There is no paid support tier and no private support commitment. Lucen is a
community open-source project; help is provided on a best-effort basis through
the public channels above. Security reports are the exception and are handled
privately and promptly per [SECURITY.md](SECURITY.md).
