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

This site hosts the developer reference and the generated API documentation.
The user-facing guides are maintained in the repository and render on GitHub:

- [README and quickstart](https://github.com/fcmv/lucen/blob/main/README.md)
- [Limitations](https://github.com/fcmv/lucen/blob/main/LIMITATIONS.md)
- [Roadmap](https://github.com/fcmv/lucen/blob/main/ROADMAP.md)
- [Stability policy](https://github.com/fcmv/lucen/blob/main/STABILITY.md)
- [Benchmarks](https://github.com/fcmv/lucen/blob/main/BENCHMARK.md)
- [Contributing](https://github.com/fcmv/lucen/blob/main/CONTRIBUTING.md)

## Reference on this site

- [Architecture](architecture.md), the pipeline and dispatch flow with diagrams
- [Pragma and clause reference](pragmas.md), every pragma and clause
- [API reference](api.md), generated from the public API
- [Glossary](glossary.md), the domain terms in one place
- [Paper](paper/lucen.md), the design and evaluation
- [Technical specification](spec/lucen_technical_spec.md)
- [Engineering guide](implementation/lucen_engineering_doc.md)
- [Formal specifications](formal/README.md)
- [Architecture decisions](adr/README.md)
