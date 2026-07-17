from __future__ import annotations

import ast
import copy
import dis

import pytest

from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import select
from lucen.codegen import generate
from lucen.execution.runtime import (
    SKIP,
    assign_path,
    audit_disjoint_dict_slabs,
    commit_dict_slab,
    commit_list_slab,
    fold_contributions,
    resolve_path,
)
from lucen.support.errors import clear_fallback_report


@pytest.fixture(autouse=True)
def _clean_report():
    clear_fallback_report()
    yield
    clear_fallback_report()


def pipeline(src):
    scan = scan_source(src, "t.py")
    analysis = analyze_source(src, scan, "t.py")[0]
    decision = select(analysis, workers=8)
    return analysis, decision, generate(analysis, decision, "t.py")


def block(body, header="for i in range(1, n):", clauses="calibrate=false"):
    body_src = "\n".join("    " + b for b in body)
    return f"# LUCEN START {clauses}\n{header}\n{body_src}\n# LUCEN END\n"


def golden(src, env):
    g = copy.deepcopy(env)
    exec(src, g)
    return g


def run_split(src, env, splits):
    analysis, _, artifact = pipeline(src)
    assert artifact is not None
    chunk_fn, _ = artifact.compile_pair()
    env = copy.deepcopy(env)
    if artifact.domain == "range":
        base = eval(ast.unparse(analysis.for_node.iter), dict(env))
        seq = None
        indices = lambda a, b: base[a:b]
    else:
        it = (
            analysis.for_node.iter.args[0]
            if artifact.domain == "enumerate"
            else analysis.for_node.iter
        )
        seq = list(eval(ast.unparse(it), dict(env)))
        indices = lambda a, b: range(a, b)
    slab_params = {p.param: p for p in artifact.slabs}
    site_params = {p for r in artifact.reductions for p in r.site_params}
    pending = []
    for a, b in splits:
        slabs, sites, args = {}, {}, []
        for p in artifact.params:
            if p == "_plx_indices":
                args.append(indices(a, b))
            elif p == "_plx_seq":
                args.append(seq)
            elif p in slab_params:
                slab = [SKIP] * (b - a) if slab_params[p].kind == "list" else {}
                slabs[p] = slab
                args.append(slab)
            elif p in site_params:
                slab = [SKIP] * (b - a)
                sites[p] = slab
                args.append(slab)
            else:
                args.append(
                    env[p]
                    if p in env
                    else __builtins__[p]
                    if isinstance(__builtins__, dict)
                    else getattr(__builtins__, p)
                )
        chunk_fn(*args)
        pending.append((a, b, slabs, sites))
    for a, b, slabs, sites in pending:
        for plan in artifact.slabs:
            container = resolve_path(env, plan.container)
            if plan.kind == "list":
                commit_list_slab(container, indices(a, b), slabs[plan.param])
            else:
                commit_dict_slab(container, slabs[plan.param])
        for red in artifact.reductions:
            cur = resolve_path(env, red.scalar)
            cur = fold_contributions(cur, [sites[p] for p in red.site_params], red.op)
            assign_path(env, red.scalar, cur)
    return env


def all_splits(n):
    yield [(0, n)]
    if n >= 1:
        yield [(k, k + 1) for k in range(n)]
    if n >= 2:
        mid = n // 2
        yield [(0, mid), (mid, n)]
    if n >= 3:
        yield [(0, 1), (1, n - 1), (n - 1, n)]
    if n >= 7:
        step = 3
        yield [(a, min(a + step, n)) for a in range(0, n, step)]


CASES = [
    (
        ["ys[i] = xs[i] * 2"],
        "for i in range(len(xs)):",
        {"xs": list(range(20)), "ys": [0] * 20},
        "ys",
    ),
    (
        ["ys[i] = xs[i]", "ys[i] += 100"],
        "for i in range(len(xs)):",
        {"xs": list(range(15)), "ys": [0] * 15},
        "ys",
    ),
    (["total += xs[i]"], "for i in range(len(xs)):", {"xs": list(range(20)), "total": 0}, "total"),
    (
        ["hi = max(hi, xs[i])"],
        "for i in range(len(xs)):",
        {"xs": [3, 1, 4, 1, 5, 9, 2, 6], "hi": 0},
        "hi",
    ),
]


@pytest.mark.parametrize("body,header,env,check", CASES)
def test_every_split_matches_sequential(body, header, env, check):
    src = block(body, header)
    expected = golden(src, env)[check]
    n = len(env[[k for k in env if k != check][0]])
    for splits in all_splits(n):
        got = run_split(src, env, splits)
        assert got[check] == expected, splits


def test_chunk_fn_never_loads_global_across_forms():
    banned = {"LOAD_GLOBAL", "LOAD_NAME", "LOAD_DEREF", "STORE_GLOBAL"}
    sources = [
        block(["ys[i] = xs[i] * s"], "for i in range(len(xs)):"),
        block(["total += xs[i]"], "for i in range(len(xs)):"),
        block(["out[i] = out[i // 2] + w[i]"]),
        block(["cache[k] = k"], "for k in keys:"),
        block(
            ["try:", "    ys[i] = 1 // xs[i]", "except ZeroDivisionError:", "    ys[i] = 0"],
            "for i in range(len(xs)):",
        ),
    ]
    for src in sources:
        _, _, artifact = pipeline(src)
        assert artifact is not None
        chunk_fn, _ = artifact.compile_pair()
        ops = {i.opname for i in dis.get_instructions(chunk_fn)}
        assert not (ops & banned), (src, ops & banned)


def test_generated_source_roundtrips_through_ast():
    for src in [
        block(["ys[i] = f(xs[i], k)"], "for i in range(len(xs)):"),
        block(["out[i] = out[i >> 1] + 1"]),
        block(
            ["if xs[i]:", "    ys[i] = g(xs[i])", "else:", "    ys[i] = 0"],
            "for i in range(len(xs)):",
        ),
    ]:
        _, _, artifact = pipeline(src)
        ast.parse(artifact.source)
        ast.parse(artifact.seq_source)


def test_chunk_and_seq_params_are_disjoint_from_specials():
    src = block(["ys[i] = xs[i] + s"], "for i in range(len(xs)):")
    _, _, artifact = pipeline(src)
    assert "_plx_j" not in artifact.params
    assert artifact.name.startswith("_plx_chunk")
    assert artifact.seq_name.startswith("_plx_seq")


def test_reduction_site_isolation_multiple_sites():
    src = block(["total += xs[i]", "total += ys[i]"], "for i in range(len(xs)):")
    env = {"xs": list(range(10)), "ys": list(range(10, 20)), "total": 5}
    got = run_split(src, env, [(0, 4), (4, 10)])
    assert got["total"] == golden(src, env)["total"]


def test_dict_audit_detects_cross_chunk_duplicate():
    slab_a = {"x": 1, "y": 2}
    slab_b = {"z": 3, "x": 9}
    assert audit_disjoint_dict_slabs([slab_a, slab_b]) == "x"
    assert audit_disjoint_dict_slabs([{"a": 1}, {"b": 2}, {"c": 3}]) is None


def test_commit_list_slab_skips_sentinels():
    container = [10, 20, 30, 40]
    commit_list_slab(container, range(1, 3), [SKIP, 99])
    assert container == [10, 20, 99, 40]


def test_commit_list_slab_contiguous_fast_path():
    container = [0, 0, 0, 0, 0]
    commit_list_slab(container, range(1, 4), [7, 8, 9])
    assert container == [0, 7, 8, 9, 0]


def test_fold_contributions_iteration_order():
    current = 0.0
    sites = [[0.1, 0.2, 0.3]]
    out = fold_contributions(current, sites, "+")
    manual = 0.0
    for v in [0.1, 0.2, 0.3]:
        manual += v
    assert out == manual


def test_selector_routed_sequential_generates_nothing():
    for src in [
        block(["out[i] = out[i - 1]"]),
        block(["out[i] = out[(i + 1) % n]"]),
        block(["out[perm[i]] = xs[i]"]),
    ]:
        _, _, artifact = pipeline(src)
        assert artifact is None
