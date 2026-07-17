from collections import deque

import pytest


def _contiguous_partitions(n):
    yield [(0, n)]
    for a in range(1, n):
        yield [(0, a), (a, n)]
        for b in range(a + 1, n):
            yield [(0, a), (a, b), (b, n)]


def _permutations(seq):
    if len(seq) <= 1:
        yield list(seq)
        return
    for i, x in enumerate(seq):
        for rest in _permutations(seq[:i] + seq[i + 1 :]):
            yield [x] + rest


def _sequential(n, write_idx, value):
    arr = {}
    for i in range(n):
        arr[write_idx(i)] = value(i)
    return arr


def _commit(chunks, write_idx, value, exec_order):
    slabs = {}
    for c in exec_order:
        start, stop = chunks[c]
        slabs[c] = [(write_idx(i), value(i)) for i in range(start, stop)]
    arr = {}
    for c in range(len(chunks)):
        for idx, val in slabs[c]:
            arr[idx] = val
    return arr


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6])
def test_privatize_commit_is_sequential_equivalent_under_all_interleavings(n):
    def write_idx(i):
        return i

    def value(i):
        return i * i + 1

    seq = _sequential(n, write_idx, value)
    checked = 0
    for chunks in _contiguous_partitions(n):
        for order in _permutations(list(range(len(chunks)))):
            assert _commit(chunks, write_idx, value, order) == seq
            checked += 1
    assert checked > 0


def test_write_set_audit_detects_cross_chunk_conflict():
    chunks = [(0, 3), (3, 6)]

    def write_idx(i):
        return i % 3

    written = [{write_idx(i) for i in range(a, b)} for a, b in chunks]
    collision = written[0] & written[1]
    assert collision, "audit must detect the shared write index, forcing sequential fallback"


def _level_of(i, c):
    k = 0
    while i > 0:
        i //= c
        k += 1
    return k


@pytest.mark.parametrize("n,c", [(4, 2), (8, 2), (9, 3), (16, 2), (10, 4)])
def test_wavefront_is_dependency_safe_deadlock_free_and_terminates(n, c):
    def dep(i):
        return i // c

    max_level = _level_of(n - 1, c)
    level_indices = {k: {i for i in range(n) if _level_of(i, c) == k} for k in range(max_level + 1)}
    done = frozenset(range(n))

    start = (frozenset(), 0)
    seen = {start}
    queue = deque([start])
    terminal = []

    while queue:
        committed, level = queue.popleft()

        for i in committed:
            assert i == 0 or dep(i) in committed

        if level == max_level + 1:
            terminal.append(committed)
            continue

        successors = []
        for i in level_indices[level]:
            if i not in committed and (i == 0 or dep(i) in committed):
                successors.append((committed | {i}, level))
        if level_indices[level] <= committed:
            successors.append((committed, level + 1))

        assert successors, f"deadlock at committed={set(committed)}, level={level}"

        for s in successors:
            if s not in seen:
                seen.add(s)
                queue.append(s)

    assert terminal
    for committed in terminal:
        assert committed == done
