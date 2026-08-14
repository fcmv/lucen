from __future__ import annotations

import pickle
import random

import pytest

from lucen.execution import _accel
from lucen.execution.runtime import (
    SKIP,
    assign_path,
    commit_list_slab,
    fold_contributions,
    resolve_path,
)


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


def test_attribute_paths_deeper_than_two_components():
    # assign_path walks parts[1:-1], which is empty for a two-component path,
    # so nothing exercised the walk until here.
    class Node:
        pass

    root, mid, leaf = Node(), Node(), Node()
    root.mid = mid
    mid.leaf = leaf
    leaf.value = 1
    env = {"root": root}

    assert resolve_path(env, "root.mid.leaf.value") == 1
    assign_path(env, "root.mid.leaf.value", 42)
    assert leaf.value == 42
    assign_path(env, "root.mid.leaf", "replaced")
    assert mid.leaf == "replaced"


@pytest.mark.parametrize("native", [False, True])
def test_contiguous_audit_rejects_a_range_past_the_total(monkeypatch, native):
    # stop > total and stop < start are independent failures; an `and` there
    # would accept a chunk running off the end. Both accel modes are checked
    # because whichever is not loaded is dead code.
    if not native:
        monkeypatch.setattr(_accel, "_native", None)
    elif _accel._native is None:
        pytest.skip("native core not loaded")
    assert _accel.audit_contiguous([(0, 4), (4, 8)], 8) is None
    # the offending chunk's own start is reported, not the runaway stop
    assert _accel.audit_contiguous([(0, 4), (4, 12)], 8) == 4
    assert _accel.audit_contiguous([(0, 4), (6, 8)], 8) == 4


def test_list_commit_takes_the_slice_path_and_never_leaks_skip():
    # The contiguous list fast path assigns the slab as a slice, so it must
    # refuse a slab holding SKIP rather than writing the sentinel through.
    container = [0, 0, 0, 0]
    commit_list_slab(container, range(0, 4), [1, 2, 3, 4])
    assert container == [1, 2, 3, 4]

    holed = [9, 9, 9, 9]
    commit_list_slab(holed, range(0, 4), [1, SKIP, 3, SKIP])
    assert holed == [1, 9, 3, 9]
    assert SKIP not in holed


def test_fold_over_no_sites_returns_the_accumulator():
    assert fold_contributions(7, [], "+") == 7


def test_the_unhandled_sentinel_is_distinct_from_none():
    # a fold over user objects may legally produce None, so "the native core did
    # not handle this" cannot itself be None
    assert _accel.UNHANDLED is not None


def test_the_accel_import_guard_honours_the_environment(monkeypatch):
    # The guard runs at import, so each branch is reached by reloading the
    # module. The lane's own state is put back at the end.
    import importlib
    import sys

    was_native, was_accelerated = _accel._native, _accel.ACCELERATED
    try:
        monkeypatch.setenv("LUCEN_DISABLE_NATIVE", "1")
        importlib.reload(_accel)
        assert _accel._native is None
        assert _accel.ACCELERATED is False

        # keyed off whether the core actually imported, not off the flag under
        # test, so the flag cannot vouch for itself
        monkeypatch.delenv("LUCEN_DISABLE_NATIVE", raising=False)
        importlib.reload(_accel)
        if _accel._native is not None:
            assert _accel.ACCELERATED is True

        # a build without the compiled core degrades instead of failing to
        # import; the parent attribute has to go too, since "from lucen import
        # _core" resolves that before it consults sys.modules
        import lucen

        monkeypatch.delattr(lucen, "_core", raising=False)
        monkeypatch.setitem(sys.modules, "lucen._core", None)
        importlib.reload(_accel)
        assert _accel._native is None
        assert _accel.ACCELERATED is False
    finally:
        monkeypatch.undo()
        importlib.reload(_accel)
    assert _accel._native is was_native
    assert _accel.ACCELERATED is was_accelerated


_FOLD_IDENTITY = {"+": 0, "*": 1, "&": 0b111, "|": 0, "^": 0, "min": 10**9, "max": -(10**9)}


@pytest.mark.parametrize("op", ["+", "*", "&", "|", "^", "min", "max"])
def test_every_advertised_fold_op_reaches_the_native_core(op):
    # Dropping an op from the accelerated set is invisible in the result, since
    # the Python twin computes the same value; only which path ran differs.
    if not _accel.ACCELERATED:
        pytest.skip("native core not loaded")
    got = _accel.fold_ordered(_FOLD_IDENTITY[op], [[3, 1, 2]], op, SKIP)
    assert got is not _accel.UNHANDLED
    assert got == _py_fold(_FOLD_IDENTITY[op], [[3, 1, 2]], op)


def test_the_native_fold_declines_what_it_cannot_handle():
    import array

    assert _accel.fold_ordered(0, [], "+", SKIP) is _accel.UNHANDLED
    assert _accel.fold_ordered(0, [[1, 2]], "concat", SKIP) is _accel.UNHANDLED
    assert _accel.fold_ordered(0, [[1, 2]], lambda a, b: a + b, SKIP) is _accel.UNHANDLED
    assert _accel.fold_ordered(0, [array.array("i", [1, 2])], "+", SKIP) is _accel.UNHANDLED


def test_integer_keyed_slabs_take_the_bitmap_audit(monkeypatch):
    # The bitmap audit is the measured fast path for index-like dict keys, and
    # it is only correct when every key is an int inside the container's length.
    if not _accel.ACCELERATED:
        pytest.skip("native core not loaded")
    from lucen.execution.runtime import audit_disjoint_dict_slabs

    seen: list = []
    real = _accel.audit_index_bitmap

    def spy(key_lists, length):
        seen.append(length)
        return real(key_lists, length)

    monkeypatch.setattr(_accel, "audit_index_bitmap", spy)
    assert audit_disjoint_dict_slabs([{0: "a", 1: "b"}, {2: "c"}], index_bound=4) is None
    assert seen == [4], "the bitmap path was not taken for integer keys"
    assert audit_disjoint_dict_slabs([{0: "a"}, {0: "b"}], index_bound=4) == 0


def test_keys_outside_the_bitmap_domain_fall_back_to_the_set_audit(monkeypatch):
    # A key at the bound is out of range for a bitmap of that length, and a
    # non-integer key is not an index at all; both must reach the set audit
    # rather than be handed to the bitmap.
    if not _accel.ACCELERATED:
        pytest.skip("native core not loaded")
    from lucen.execution.runtime import audit_disjoint_dict_slabs

    seen: list = []
    monkeypatch.setattr(_accel, "audit_index_bitmap", lambda *args: seen.append(args))
    assert audit_disjoint_dict_slabs([{4: "x"}], index_bound=4) is None
    assert audit_disjoint_dict_slabs([{"a": 1}], index_bound=4) is None
    assert audit_disjoint_dict_slabs([{"a": 1}, {"a": 2}], index_bound=4) == "a"
    assert seen == []


def test_skip_is_a_singleton_with_a_stable_marker():
    # SKIP marks an iteration that wrote nothing, so it has to stay
    # distinguishable from every value a chunk could legitimately produce, and
    # keep its identity across the process boundary: a slab that came back with
    # a copy would commit the marker instead of leaving the slot alone.
    from lucen.execution.runtime import _Skip

    assert _Skip() is SKIP
    assert pickle.loads(pickle.dumps(SKIP)) is SKIP
    assert repr(SKIP) == "<lucen.SKIP>"


def test_new_list_slab_is_prefilled_with_the_marker():
    from lucen.execution.runtime import new_list_slab

    assert new_list_slab(3) == [SKIP, SKIP, SKIP]
    assert new_list_slab(0) == []


def test_contiguous_buffer_slab_commits_as_one_slice_store():
    # The buffer fast path exists to replace n per-element stores with a single
    # slice store. A strided range must not reach it: the slice would pack the
    # values into consecutive slots instead of the ones the loop wrote.
    class Counting(bytearray):
        writes = 0

        def __setitem__(self, index, value):
            type(self).writes += 1
            bytearray.__setitem__(self, index, value)

    Counting.writes = 0
    target = Counting(b"\x00" * 6)
    commit_list_slab(target, range(1, 5), bytearray(b"\x01\x02\x03\x04"))
    assert bytes(target) == b"\x00\x01\x02\x03\x04\x00"
    assert Counting.writes == 1

    Counting.writes = 0
    strided = Counting(b"\x00" * 6)
    commit_list_slab(strided, range(0, 6, 2), bytearray(b"\x07\x08\x09"))
    assert bytes(strided) == b"\x07\x00\x08\x00\x09\x00"
    assert Counting.writes == 3


def test_strided_list_commit_uses_the_element_loop():
    target = [0] * 6
    commit_list_slab(target, range(0, 6, 2), [7, 8, 9])
    assert target == [7, 0, 8, 0, 9, 0]
