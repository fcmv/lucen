# Stability and Compatibility Policy

Lucen is more than a library API: it defines a small language surface (the
pragma grammar, the clause vocabulary, the `lucen.toml` schema) that user
code and projects depend on. This document states what is stable, what is not,
and how changes are made, so that upgrading Lucen is predictable.

Lucen follows [Semantic Versioning](https://semver.org/). A release is
`MAJOR.MINOR.PATCH`. The sections below define what a change to each part of the
surface means in those terms.

## The one guarantee that never changes

Independent of any version, a parallel run is bit-identical to the same file
executed as plain sequential Python. This is not a versioned feature; it is the
definition of the project. No release, major or otherwise, will relax it. A
release that changed it would not be a new version of Lucen.

## Stable surface (changes are breaking, so MAJOR)

These are the contracts user code depends on. A backward-incompatible change to
any of them is a major-version change, announced in the release notes with a
migration path.

- **The pragma grammar.** `# LUCEN START`, `# LUCEN END`, and
  `# LUCEN TRUST`, and the rule that they are ordinary comments. The keyword
  is stable and is deliberately the project's own name, which a stray comment is
  unlikely to contain.
- **The clause vocabulary.** The names and accepted forms of the pragma clauses
  (`backend`, `calibrate`, `trust`, `depend`, `reduce`, `timeout`, `on_error`,
  `strict`, `grainsize`, `progress`, and the rest). Removing a clause or changing
  the meaning of an accepted value is breaking.
- **The `lucen.toml` schema.** Section and key names, and their accepted
  values, including the precedence chain.
- **The public Python API.** `lucen.activate`, `lucen.deactivate`,
  `lucen.get_fallback_report`, and the documented fields of the fallback
  records. The public exception types, which are re-exported at the top level
  (`lucen.LucenError`, `lucen.ClauseValueError`, and the parallel
  runtime errors); catch them from there rather than from an internal module.
- **The CLI contract.** The `lucen explain` and `lucen profile`
  subcommands, their documented flags, and the `--strict --baseline` gate
  behavior. The human-readable text of reports is not part of this contract (see
  below).

## Additive surface (changes are MINOR)

New clauses, new clause forms, new config keys, new CLI flags, new experimental
flags, and new backends are added in minor releases. Adding a capability that
does not change the behavior of existing marked code is a minor change. A block
that parallelized before continues to parallelize the same way.

## Not stable (may change in any release)

These are deliberately outside the compatibility contract. Depending on them is
depending on an implementation detail.

- **The native core.** `lucen._core` and everything in the `lucen_core`
  crate are internal. The pure-Python fallback and the native path are
  guaranteed to produce identical results, but the native module's symbols,
  signatures, and existence are not a public API.
- **Generated code.** The chunk functions and sequential twins emitted by
  codegen, and the on-disk rewrite cache format.
- **Any name prefixed with an underscore**, and any module not re-exported from
  the top-level `lucen` package.
- **The exact wording of reports.** The text of `explain`, `profile`, and
  fallback-report messages may be improved at any time. Match on the structured
  fields and error types, not on message strings.
- **Routing and cost-model decisions.** Which backend the gate picks for a given
  block, and the profitability thresholds, may change as the cost model is
  refined. The result stays bit-identical; only the path to it may differ. Pin a
  backend with `backend=` if you need a specific one.

## Deprecation policy

A stable-surface feature is not removed abruptly. It is first deprecated: still
functional, documented as deprecated in the release notes, for at least one
minor release before removal in the next major. Where a clean automated
replacement exists, a removed clause fails loud with a message naming its
replacement rather than being silently ignored (for example, the removed
`process_wait` and the renamed `batch_size` clauses raise with a pointer to the
current form).

## Supported interpreters

- **Python.** CPython 3.9 and later, including the free-threaded builds. The
  minimum supported version is raised only in a minor release, announced in the
  release notes, and chosen conservatively (an end-of-life CPython may be dropped
  to allow using newer language features).
- **Rust (for building the native core).** The core targets a recent stable
  Rust toolchain. Building from source uses current stable Rust; the minimum
  supported Rust version is raised as needed and is not itself a stability
  guarantee to downstream users, who consume the prebuilt wheel.

## Wheels and the native core

Two wheels are published for each release. A native `abi3` build loads on every
CPython 3.9 through 3.14 GIL build from one binary per platform. A
`py3-none-any` pure-Python wheel covers everywhere the native wheel is not
installable: free-threaded interpreters (which have no stable ABI, so the abi3
wheel cannot be selected there) and any architecture without a native build.
`pip install lucen` picks the native wheel on GIL builds and the pure wheel
otherwise, so the install always succeeds and always runs correctly; the
presence or absence of the native core never changes results. Free-threaded
native acceleration is planned (ROADMAP M2); until then a free-threaded install
uses the pure wheel and the pure-Python fallback.
