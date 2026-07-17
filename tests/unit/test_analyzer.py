from __future__ import annotations

import pytest

from lucen.analysis.analyzer import DependencyShape, resolve_shapes
from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import scan_source
from lucen.support.errors import clear_fallback_report


@pytest.fixture(autouse=True)
def _clean_report():
    clear_fallback_report()
    yield
    clear_fallback_report()


def shapes_for(body_line: str, header: str = "for i in range(1, n):"):
    src = f"# LUCEN START\n{header}\n    {body_line}\n# LUCEN END\n"
    scan = scan_source(src, "t.py")
    analyses = analyze_source(src, scan, "t.py")
    assert len(analyses) == 1 and analyses[0].ok
    return resolve_shapes(analyses[0])


def test_self_contained():
    result = shapes_for("results[i] = scale(results[i])")["results"]
    assert result.shape is DependencyShape.SELF_CONTAINED


def test_monotonic_offset():
    result = shapes_for("results[i] = results[i - 1] + values[i]")["results"]
    assert result.shape is DependencyShape.MONOTONIC_OFFSET
    assert result.offset == 1


def test_monotonic_larger_offset():
    result = shapes_for("results[i] = results[i - 3]")["results"]
    assert result.offset == 3


@pytest.mark.parametrize(
    "index,divisor",
    [
        ("i // 2", 2),
        ("i // 16", 16),
        ("i // (2 ** 3)", 8),
        ("i >> 1", 2),
        ("i >> 4", 16),
    ],
)
def test_recognized_dag_and_shorthands(index, divisor):
    result = shapes_for(f"results[i] = combine(results[{index}], w[i])")["results"]
    assert result.shape is DependencyShape.RECOGNIZED_DAG
    assert result.divisor == divisor


@pytest.mark.parametrize(
    "index",
    [
        "(i + 1) % n",
        "(i - 2) % n",
        "(i + 7) % 16",
    ],
)
def test_modular_self_reference_is_a_cycle(index):
    result = shapes_for(f"results[i] = results[{index}]")["results"]
    assert result.shape is DependencyShape.MODULAR_SELF_REFERENCE


@pytest.mark.parametrize(
    "index",
    [
        "i + 1",
        "i // 1",
        "i >> 0",
        "i // c",
        "i - k",
        "perm[i]",
        "(i + 0) % n",
        "i * 2",
        "2 ** i",
    ],
)
def test_near_misses_fall_to_unresolved(index):
    result = shapes_for(f"results[i] = results[{index}]")["results"]
    assert result.shape is DependencyShape.UNRESOLVED


def test_multiple_dag_reads_take_worst_divisor():
    result = shapes_for("results[i] = f(results[i // 4], results[i // 2])")["results"]
    assert result.shape is DependencyShape.RECOGNIZED_DAG
    assert result.divisor == 2


def test_self_read_combines_with_dag():
    result = shapes_for("results[i] = f(results[i], results[i // 2])")["results"]
    assert result.shape is DependencyShape.RECOGNIZED_DAG


def test_monotonic_dominates_dag():
    result = shapes_for("results[i] = f(results[i - 1], results[i // 2])")["results"]
    assert result.shape is DependencyShape.MONOTONIC_OFFSET


def test_unresolved_write_index_blocks_recognition():
    result = shapes_for("results[i + 1] = results[i]")
    assert "results" not in result or result["results"].shape is DependencyShape.UNRESOLVED


def test_value_domain_loop_yields_unresolved():
    result = shapes_for("acc[x] = acc[x - 1]", header="for x in xs:")["acc"]
    assert result.shape is DependencyShape.UNRESOLVED


def test_only_raw_targets_are_resolved():
    shapes = shapes_for("results[i] = values[i] * 2")
    assert shapes == {}
