---
name: Bug report
about: A reproducible defect, including any parallel result that differs from sequential
title: ""
labels: bug
assignees: ""
---

<!--
Before filing: if this is a suspected SECURITY issue, especially a silent wrong
result reachable from ordinary code, do NOT file it here. Use the private
channel in SECURITY.md instead.

The single most useful bug report for a correctness-first parallelizer is one
that makes a divergence concrete. Please fill in as much as you can.
-->

## What happened

<!-- A clear description of the behavior you saw. -->

## Minimal reproduction

<!-- The smallest marked source file that triggers it. A reproduction a
maintainer can run and watch fail is worth more than any description. -->

```python
import lucen
lucen.activate()

# ... your minimal marked block ...
```

## Sequential vs parallel

<!-- For a wrong or unexpected RESULT, this is the most important section. -->

- **Result with Lucen activated:**
- **Result with Lucen NOT activated** (the same file, pragmas treated as
  comments):

<!-- If these two differ and you did NOT use `depend=none` with
`skip_runtime_check=true`, and no stateful/unreadable helper is involved, this
is a high-priority correctness issue. -->

## Environment

- Lucen version:
- Python version and build (for example, CPython 3.12, GIL build):
- Operating system:
- Native core or pure-Python fallback? (run with `LUCEN_DISABLE_NATIVE=1` to
  force the fallback; note whether the behavior changes):

## `lucen explain` output

<!-- Optional but very helpful: paste `lucen explain yourfile.py` for the
block in question. -->
