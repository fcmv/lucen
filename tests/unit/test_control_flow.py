from __future__ import annotations

import array
import ast
import copy

import pytest

from lucen.analysis.rewriter import Classification, analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import Eligibility, select
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
    art = generate(a, d, "t.py")
    return a, d, (make_spec(a, d, art) if art else None), art


def run(src, env, backend="thread"):
    a, d, spec, _ = build(src)
    assert spec is not None
    env = copy.deepcopy(env)
    it = a.for_node.iter
    if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "enumerate":
        iterable = eval(ast.unparse(it.args[0]), dict(env))
    else:
        iterable = eval(ast.unparse(it), dict(env))
    execute(spec, iterable, env, force_backend=backend)
    return env


def golden(src, env):
    g = copy.deepcopy(env)
    exec(src, g)
    return g


def block(body, header="for i in range(len(xs)):", clauses="calibrate=false"):
    body_src = "\n".join("    " + b for b in body)
    return f"# LUCEN START {clauses}\n{header}\n{body_src}\n# LUCEN END\n"


def test_while_inside_for():
    src = block(
        ["n = xs[i]", "c = 0", "while n > 0:", "    n = n // 2", "    c = c + 1", "out[i] = c"]
    )
    env = {"xs": [1, 7, 64, 255, 3, 8], "out": [0] * 6}
    assert run(src, env)["out"] == golden(src, env)["out"]


def test_if_elif_else_inside_for():
    src = block(
        [
            "if xs[i] > 100:",
            "    out[i] = 3",
            "elif xs[i] > 10:",
            "    out[i] = 2",
            "elif xs[i] > 0:",
            "    out[i] = 1",
            "else:",
            "    out[i] = 0",
        ]
    )
    env = {"xs": [200, 50, 5, -1, 11, 0], "out": [-9] * 6}
    assert run(src, env)["out"] == golden(src, env)["out"]


def test_try_except_else_finally_inside_for_parallelizes():
    src = block(
        [
            "try:",
            "    out[i] = 100 // xs[i]",
            "except ZeroDivisionError:",
            "    out[i] = 0",
            "else:",
            "    marks[i] = 1",
            "finally:",
            "    out[i] = out[i] + 0",
        ]
    )
    a, d, spec, _ = build(src)
    assert d.eligibility is not Eligibility.SEQUENTIAL
    env = {"xs": [1, 2, 0, 4, 0, 5], "out": [-1] * 6, "marks": [0] * 6}
    got = run(src, env)
    g = golden(src, env)
    assert got["out"] == g["out"] and got["marks"] == g["marks"]


def test_continue_inside_for():
    src = block(["if xs[i] < 0:", "    continue", "out[i] = xs[i] * 2"])
    env = {"xs": [1, -2, 3, -4, 5], "out": [0] * 5}
    assert run(src, env)["out"] == golden(src, env)["out"]


def test_nested_if_in_while_parallelizes():
    src = block(
        [
            "n = xs[i]",
            "steps = 0",
            "while n > 1:",
            "    if n % 2 == 0:",
            "        n = n // 2",
            "    else:",
            "        n = 3 * n + 1",
            "    steps = steps + 1",
            "out[i] = steps",
        ]
    )
    a, d, spec, art = build(src)
    assert art is not None and d.eligibility is Eligibility.THREAD_CAPABLE
    env = {"xs": [6, 7, 27, 1, 9, 3], "out": [0] * 6}
    assert run(src, env)["out"] == golden(src, env)["out"]


def test_marked_while_loop_falls_back():
    src = "# LUCEN START calibrate=false\nwhile i < n:\n    total += i\n    i += 1\n# LUCEN END\n"
    scan = scan_source(src, "t.py")
    a = analyze_source(src, scan, "t.py")[0]
    assert not a.ok


def test_nested_for_parallelizes():
    src = block(
        ["s = 0", "for v in rows[i]:", "    s += v", "out[i] = s"],
        header="for i in range(len(rows)):",
    )
    a, d, spec, art = build(src)
    assert art is not None and d.eligibility is Eligibility.THREAD_CAPABLE
    env = {"rows": [[1, 2, 3], [4, 5], [6], [7, 8, 9, 10]], "out": [0] * 4}
    assert run(src, env)["out"] == golden(src, env)["out"]


def test_nested_for_cross_iteration_reduction_falls_back():
    src = block(["for v in rows[i]:", "    total += v"], header="for i in range(len(rows)):")
    a, d, spec, art = build(src)
    assert art is None


NESTED = [
    (
        ["s = 0", "for v in rows[i]:", "    s += v", "out[i] = s"],
        {"rows": [[1, 2, 3], [4, 5], [], [6, 7, 8, 9]], "out": [0] * 4},
        "for i in range(len(rows)):",
    ),
    (
        [
            "n = xs[i]",
            "c = 0",
            "while n > 0:",
            "    n = n // 2",
            "    c += 1",
            "if c > 2:",
            "    out[i] = c * 10",
            "else:",
            "    out[i] = c",
        ],
        {"xs": [1, 7, 64, 255, 3, 8, 1000], "out": [0] * 7},
        "for i in range(len(xs)):",
    ),
    (
        [
            "if xs[i] > 0:",
            "    for k in range(xs[i]):",
            "        if k % 2 == 0:",
            "            out[i] += k",
            "else:",
            "    out[i] = -1",
        ],
        {"xs": [3, -1, 5, 0, 4], "out": [0] * 5},
        "for i in range(len(xs)):",
    ),
    (
        [
            "for j in range(len(rows[i])):",
            "    try:",
            "        out[i] += 100 // rows[i][j]",
            "    except ZeroDivisionError:",
            "        out[i] += 0",
        ],
        {"rows": [[1, 2, 0], [4, 5], [0, 0, 1]], "out": [0] * 3},
        "for i in range(len(rows)):",
    ),
    (
        [
            "s = 0",
            "for v in rows[i]:",
            "    if v < 0:",
            "        continue",
            "    s += v",
            "out[i] = s",
        ],
        {"rows": [[1, -2, 3], [-4, -5], [6, 7]], "out": [0] * 3},
        "for i in range(len(rows)):",
    ),
    (
        [
            "first = -1",
            "for j in range(len(rows[i])):",
            "    if rows[i][j] > 5:",
            "        first = rows[i][j]",
            "        break",
            "out[i] = first",
        ],
        {"rows": [[1, 2, 9, 3], [4, 5], [7, 8]], "out": [0] * 3},
        "for i in range(len(rows)):",
    ),
    (
        [
            "acc = 0",
            "for v in rows[i]:",
            "    m = v",
            "    while m > 1:",
            "        if m % 3 == 0:",
            "            m = m // 3",
            "        else:",
            "            m = m - 1",
            "        acc += 1",
            "out[i] = acc",
        ],
        {"rows": [[9, 4], [27], [2, 3, 5]], "out": [0] * 3},
        "for i in range(len(rows)):",
    ),
]


@pytest.mark.parametrize("body,env,header", NESTED)
def test_nested_combination_matches_sequential(body, env, header):
    src = block(body, header=header)
    a, d, spec, art = build(src)
    assert art is not None, "expected the nested block to be parallel-eligible"
    g = golden(src, env)
    for backend in ("thread", "process"):
        got = run(src, copy.deepcopy(env), backend=backend)
        assert all(got[k] == g[k] for k in env), (backend, body)


def test_nested_element_mutation_with_self_read():
    src = block(
        ["for j in range(len(grid[i])):", "    grid[i][j] = grid[i][j] * 2"],
        header="for i in range(len(grid)):",
    )
    a, d, spec, art = build(src)
    assert art is not None and art.inplace_mutation
    env = {"grid": [[1, 2, 3], [4, 5], [6, 7, 8, 9]]}
    assert run(src, env)["grid"] == golden(src, env)["grid"]


def test_stale_value_across_nested_loop_falls_back():
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(len(rows)):\n"
        "    for v in rows[i]:\n"
        "        tmp = v\n"
        "    out[i] = tmp\n"
        "# LUCEN END\n"
    )
    a, d, spec, art = build(src)
    assert art is None


def test_cross_branch_value_is_not_loop_local():
    src = block(["if xs[i] > 0:", "    x = xs[i]", "if xs[i] < 5:", "    out[i] = x"])
    analysis = analyze_source(src, scan_source(src, "t.py"), "t.py")[0]
    assert analysis.targets["x"].classification is not Classification.LOOP_LOCAL


def test_inner_index_write_falls_back():
    src = (
        "# LUCEN START calibrate=false\n"
        "for i in range(n):\n"
        "    for j in range(n):\n"
        "        grid[j] += 1\n"
        "# LUCEN END\n"
    )
    a, d, spec, art = build(src)
    assert art is None


def test_while_condition_on_shared_falls_back():
    src = block(["out[i] = xs[i]", "while out[i] < 100:", "    out[i] = out[i] * 2"])
    a, d, spec, art = build(src)
    assert art is None


def test_outer_break_still_sequential_under_nesting():
    src = block(["for k in range(3):", "    ys[i] += k", "if xs[i] < 0:", "    break"])
    a, d, spec, art = build(src)
    assert a.has_break
    assert d.eligibility is Eligibility.SEQUENTIAL


def test_nested_reduction_with_reduce_clause_still_sequential():
    src = block(
        ["for v in rows[i]:", "    total += v"],
        header="for i in range(len(rows)):",
        clauses="calibrate=false, reduce=custom(fn=operator.add, identity=0)",
    )
    a, d, spec, art = build(src)
    assert d.eligibility is Eligibility.SEQUENTIAL
    assert art is None


def test_mixed_read_write_branch_parallelizes():
    src = block(["if flags[i]:", "    out[i] = xs[i]", "else:", "    out[i] = out[i] + 1"])
    a, d, spec, _ = build(src)
    assert a.targets["out"].classification is Classification.READ_AFTER_WRITE
    assert d.eligibility is Eligibility.THREAD_CAPABLE
    env = {"xs": list(range(60)), "flags": [i % 2 for i in range(60)], "out": [10] * 60}
    assert run(src, env)["out"] == golden(src, env)["out"]


def test_disagreeing_write_index_still_conflicts():
    src = block(["if flags[i]:", "    out[i] = xs[i]", "else:", "    out[i - 1] = xs[i]"])
    a, d, spec, art = build(src)
    assert art is None
    assert any(r.error == "BranchMergeConflictError" for r in a.fallbacks)


def test_nested_grid_two_columns():
    src = block(["grid[i][0] = xs[i]", "grid[i][1] = xs[i] * xs[i]"])
    env = {"xs": list(range(50)), "grid": [[0, 0] for _ in range(50)]}
    assert run(src, env)["grid"] == golden(src, env)["grid"]


def test_array_output_through_slab_path():
    src = block(["out[i] = xs[i] * 2.0", "total += xs[i]"])
    n = 300
    env = {"xs": [float(k) for k in range(n)], "out": array.array("d", [0.0] * n), "total": 0.0}
    got = run(src, env, backend="process")
    g = golden(src, env)
    assert list(got["out"]) == list(g["out"])
    assert got["total"] == g["total"]
