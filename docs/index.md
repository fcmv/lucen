# Lucen

Lucen is a source-to-source compiler that automatically parallelizes
ordinary Python loops using comment pragmas. It parallelizes only the loops it
can prove are both safe and worthwhile, and guarantees that a parallel run is
bit-identical to the same file executed as plain sequential Python.

```python
import lucen
lucen.activate()
```

```python
# LUCEN START
for i in range(len(records)):
    scores[i] = score(records[i])
# LUCEN END
```

The quickstart is the
[README](https://github.com/fcmv/lucen/blob/main/README.md). Everything else is
on this site.

## Guides

- [Limitations](limitations.md), known gaps and the trust contract
- [Benchmarks](benchmark.md), measurements across seven interpreters
- [Stability policy](stability.md), what is stable and what may change
- [Roadmap](roadmap.md), what is planned and in what order
- [Changelog](changelog.md), what changed in each release

## Reference

- [Architecture](architecture.md), the pipeline and dispatch flow with diagrams
- [Pragma and clause reference](pragmas.md), every pragma and clause
- [API reference](api.md), generated from the public API
- [Glossary](glossary.md), the domain terms in one place
- [Paper](paper/lucen.md), the design and evaluation
- [Technical specification](spec/lucen_technical_spec.md)
- [Engineering guide](implementation/lucen_engineering_doc.md)
- [Formal specifications](formal/README.md)
- [Architecture decisions](adr/README.md)

## Project

- [Getting support](support.md), where to take a question, a bug, or a security report
- [Contributing](contributing.md), how to get a change merged
