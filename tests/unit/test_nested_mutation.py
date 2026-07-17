from __future__ import annotations

import pytest

from lucen.analysis.rewriter import Classification, analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import select
from lucen.codegen import generate
from lucen.execution import dispatch
from lucen.execution.dispatch import execute, make_spec
from lucen.support import config
from lucen.support.errors import ErrorsMode, clear_fallback_report, set_errors_mode


@pytest.fixture(autouse=True)
def _clean_state():
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config())
    yield
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config())


def build(src):
    scan = scan_source(src, "t.py")
    a = analyze_source(src, scan, "t.py")[0]
    d = select(a, workers=8)
    return a, make_spec(a, d, generate(a, d, "t.py"))


class Cell:
    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return isinstance(other, Cell) and self.value == other.value


NESTED_SUBSCRIPT = (
    "# LUCEN START calibrate=false\nfor i in range(len(xs)):\n    grid[i][0] = xs[i]\n# LUCEN END\n"
)

ATTRIBUTE = (
    "# LUCEN START calibrate=false\n"
    "for i in range(len(xs)):\n"
    "    objs[i].value = objs[i].value * 2\n# LUCEN END\n"
)


def test_nested_subscript_flagged_inplace():
    a, _ = build(NESTED_SUBSCRIPT)
    assert a.has_inplace_mutation
    assert a.targets["grid"].in_place
    assert a.targets["grid"].classification is Classification.SHARED_INDEXED_SAFE


def test_attribute_write_flagged_inplace():
    a, _ = build(ATTRIBUTE)
    assert a.has_inplace_mutation
    assert a.targets["objs"].in_place


def test_inplace_blocks_pick_thread(monkeypatch):
    _, spec = build(NESTED_SUBSCRIPT)
    monkeypatch.setattr(dispatch, "free_threaded", lambda: False)
    assert dispatch._pick_backend(spec) == "thread"


def test_nested_subscript_write_correct():
    _, spec = build(NESTED_SUBSCRIPT)
    n = 120
    grid = [[0, 0] for _ in range(n)]
    env = {"xs": list(range(100, 100 + n)), "grid": grid}
    execute(spec, range(n), env, force_backend="thread")
    assert env["grid"] == [[100 + k, 0] for k in range(n)]


def test_attribute_element_write_correct():
    _, spec = build(ATTRIBUTE)
    n = 100
    objs = [Cell(k) for k in range(n)]
    env = {"objs": objs}
    execute(spec, range(n), env, force_backend="thread")
    assert [o.value for o in env["objs"]] == [k * 2 for k in range(n)]


def test_two_column_grid_write_correct():
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(len(xs)):\n"
        "    grid[i][0] = xs[i]\n"
        "    grid[i][1] = xs[i] * xs[i]\n# LUCEN END\n"
    )
    _, spec = build(src)
    n = 90
    grid = [[0, 0] for _ in range(n)]
    env = {"xs": list(range(n)), "grid": grid}
    execute(spec, range(n), env, force_backend="thread")
    assert env["grid"] == [[k, k * k] for k in range(n)]


def test_plain_indexed_write_not_flagged():
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(len(xs)):\n"
        "    holder.data[i] = xs[i]\n# LUCEN END\n"
    )
    a, _ = build(src)
    assert not a.has_inplace_mutation
    assert not a.targets["holder.data"].in_place
