import sys

import pytest

_GRAALPY = sys.implementation.name == "graalpy"

# A handful of tests assert CPython implementation details rather than the
# bit-identity guarantee: two disassemble the generated chunk function with the
# `dis` module (unavailable on GraalPy), one asserts the cost-model probe's
# routing decision (tuned to CPython's execution profile), and one exercises the
# CPython buffer fast path. GraalPy is a different VM and support for it is
# best-effort (experimental); the rest of the suite, which does run there,
# covers correctness. Skip only these, and only on GraalPy.
_GRAALPY_SKIP = frozenset(
    {
        "test_chunk_function_has_zero_global_loads",
        "test_chunk_fn_never_loads_global_across_forms",
        "test_probe_gates_tiny_block_and_stays_correct",
        "test_bytearray_output",
    }
)


def pytest_collection_modifyitems(config, items):
    if not _GRAALPY:
        return
    skip = pytest.mark.skip(
        reason="CPython implementation detail; not applicable to GraalPy (experimental)"
    )
    for item in items:
        if item.name in _GRAALPY_SKIP:
            item.add_marker(skip)
