import sys

import pytest

_GRAALPY = sys.implementation.name == "graalpy"

# These tests assert CPython implementation details (bytecode shape via `dis`,
# the CPython-tuned cost-model probe, the buffer fast path), not the bit-identity
# guarantee that the rest of the suite still checks on GraalPy. GraalPy is a
# different VM and best-effort, so skip only these, and only there.
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
