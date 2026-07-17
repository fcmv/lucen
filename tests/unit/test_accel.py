from __future__ import annotations

import random

import pytest

from lucen.execution import _accel
from lucen.execution.runtime import SKIP, fold_contributions


def py_bitmap_audit(chunk_lists, length):
    seen = set()
    for indices in chunk_lists:
        local = set()
        for idx in indices:
            if idx in local:
                return idx
            local.add(idx)
        if not seen.isdisjoint(local):
            for idx in indices:
                if idx in seen:
                    return idx
        seen.update(local)
    return None


@pytest.mark.parametrize("seed", range(20))
def test_bitmap_audit_matches_python(seed):
    rng = random.Random(seed)
    length = rng.choice([1, 64, 65, 200, 1000])
    n_chunks = rng.randint(1, 6)
    chunks = []
    for _ in range(n_chunks):
        size = rng.randint(0, length)
        chunks.append(rng.sample(range(length), size))
    if rng.random() < 0.5 and len(chunks) >= 2 and chunks[0]:
        chunks[-1] = chunks[-1] + [chunks[0][0]]
    expected = py_bitmap_audit(chunks, length)
    got = _accel.audit_index_bitmap(chunks, length)
    assert (got is None) == (expected is None)
    if got is not None:
        flat = [i for c in chunks for i in c]
        assert flat.count(got) >= 2 or any(c.count(got) >= 2 for c in chunks)


def test_bitmap_in_chunk_duplicate():
    assert _accel.audit_index_bitmap([[1, 2, 2]], 3) == 2


def test_bitmap_out_of_range_guard():
    if _accel.ACCELERATED:
        with pytest.raises(ValueError):
            _accel.audit_index_bitmap([[5]], 3)


@pytest.mark.parametrize("seed", range(20))
def test_contiguous_audit_matches_python(seed):
    rng = random.Random(seed + 50)
    total = rng.choice([1, 10, 64, 257])
    n_chunks = rng.randint(1, 8)
    step = max(1, -(-total // n_chunks))
    ranges = [(a, min(a + step, total)) for a in range(0, total, step)]
    assert _accel.audit_contiguous(ranges, total) is None
    if len(ranges) >= 2:
        broken = ranges[:-1] + [(ranges[-1][0] + 1, ranges[-1][1] + 1)]
        assert _accel.audit_contiguous(broken, total) is not None


def test_contiguous_audit_handles_empty_chunks_with_tied_starts():
    for ranges, total in [
        ([(0, 1), (0, 0)], 1),
        ([(0, 0), (0, 1)], 1),
        ([(0, 0), (0, 0), (0, 2)], 2),
        ([(0, 2), (2, 2), (2, 5)], 5),
    ]:
        assert _accel.audit_contiguous(list(ranges), total) is None
        native = _accel.audit_contiguous(list(ranges), total)
        _native = _accel._native
        _accel._native = None
        try:
            fallback = _accel.audit_contiguous(list(ranges), total)
        finally:
            _accel._native = _native
        assert native == fallback


def test_fold_empty_sites():
    assert fold_contributions(3.5, [], "+") == 3.5


def test_accel_reports_state():
    assert isinstance(_accel.ACCELERATED, bool)
    if _accel.ACCELERATED:
        assert _accel.core_version() is not None


def test_fallback_path_matches_native(monkeypatch):
    rng = random.Random(7)
    length = 500
    chunks = [rng.sample(range(length), rng.randint(0, length)) for _ in range(4)]
    native_result = _accel.audit_index_bitmap(chunks, length)
    monkeypatch.setattr(_accel, "_native", None)
    fallback_result = _accel.audit_index_bitmap(chunks, length)
    assert (native_result is None) == (fallback_result is None)


def test_fallback_contiguous(monkeypatch):
    monkeypatch.setattr(_accel, "_native", None)
    assert _accel.audit_contiguous([(0, 3), (3, 8)], 8) is None
    assert _accel.audit_contiguous([(0, 3), (4, 8)], 8) == 3


def _py_fold(current, slabs, op):
    from lucen.execution.runtime import _FOLD

    combine = _FOLD[op]
    for j in range(len(slabs[0]) if slabs else 0):
        for slab in slabs:
            v = slab[j]
            if v is not SKIP:
                current = combine(current, v)
    return current


@pytest.mark.parametrize(
    "op,current,values",
    [
        ("+", 0.5, [0.1 * k + 0.003 for k in range(500)]),
        ("+", 10**30, [10**25 + k for k in range(200)]),
        ("+", 0, list(range(300))),
        ("*", 1, [1, 2, 3, 1, 2, 1, 4]),
        ("min", 10**9, [5, -3, 5, 7, -3]),
        ("max", -(10**9), [5, 7, 7, 2]),
        ("&", 0b1111, [0b1101, 0b0111]),
        ("|", 0, [1, 4, 16]),
        ("^", 0, [3, 5, 9, 5]),
    ],
)
def test_fold_ordered_matches_python(op, current, values):
    slab = list(values)
    slab[len(slab) // 2 : len(slab) // 2] = [SKIP, SKIP]
    got = fold_contributions(current, [slab], op)
    expected = _py_fold(current, [slab], op)
    assert got == expected
    if isinstance(expected, float):
        assert repr(got) == repr(expected)


def test_fold_ordered_multi_site_order():
    a = [1.0, SKIP, 3.0]
    b = [10.0, 20.0, SKIP]
    got = fold_contributions(0.0, [a, b], "+")
    assert repr(got) == repr(((((0.0 + 1.0) + 10.0) + 20.0) + 3.0))


def test_commit_calls_user_setitem_exactly_once_each():
    class Counting(list):
        writes = 0

        def __setitem__(self, i, v):
            type(self).writes += 1
            list.__setitem__(self, i, v)

    from lucen.execution.runtime import commit_list_slab

    Counting.writes = 0
    target = Counting([0] * 10)
    slab = [1, SKIP, 3, SKIP, 5]
    commit_list_slab(target, range(2, 7), slab)
    assert list(target) == [0, 0, 1, 0, 3, 0, 5, 0, 0, 0]
    assert Counting.writes == 3


def test_commit_gap_semantics_reference():
    from lucen.execution.runtime import commit_list_slab

    slab = [7, SKIP, 9, 10, SKIP, 12]
    target = [0] * 12
    commit_list_slab(target, range(3, 9), slab)
    py_target = [0] * 12
    for pos, value in zip(range(3, 9), slab):
        if value is not SKIP:
            py_target[pos] = value
    assert target == py_target


def test_fold_ordered_unhandled_sentinel_for_custom_op():
    calls = []

    def weird(a, b):
        calls.append(b)
        return None

    out = fold_contributions(None, [[1, 2, 3]], weird)
    assert out is None and calls == [1, 2, 3]
