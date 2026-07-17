from __future__ import annotations

import ast
import builtins as _builtins
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
from lucen.support.errors import clear_fallback_report, get_fallback_report


@pytest.fixture(autouse=True)
def _clean_report():
    clear_fallback_report()
    yield
    clear_fallback_report()


def pipeline(src: str):
    scan = scan_source(src, "t.py")
    analyses = analyze_source(src, scan, "t.py")
    assert len(analyses) == 1
    analysis = analyses[0]
    decision = select(analysis, workers=8)
    artifact = generate(analysis, decision, "t.py")
    return analysis, decision, artifact


def golden(src: str, env: dict) -> dict:
    g = copy.deepcopy(env)
    exec(src, g)
    return g


def run_parallel(
    src: str, env: dict, chunks: int = 3, commit_each: bool = False, bounds=None
) -> dict:
    analysis, _, artifact = pipeline(src)
    assert artifact is not None
    env = copy.deepcopy(env)
    chunk_fn, _ = artifact.compile_pair()
    if artifact.domain == "range":
        base = eval(ast.unparse(analysis.for_node.iter), dict(env))
        seq = None
        total = len(base)
        indices = lambda a, b: base[a:b]
    else:
        it_expr = (
            analysis.for_node.iter.args[0]
            if artifact.domain == "enumerate"
            else analysis.for_node.iter
        )
        seq = list(eval(ast.unparse(it_expr), dict(env)))
        total = len(seq)
        indices = lambda a, b: range(a, b)
    if bounds is None:
        step = max(1, -(-total // chunks))
        bounds = [(a, min(a + step, total)) for a in range(0, total, step)]

    slab_params = {p.param: p for p in artifact.slabs}
    site_params = {p for r in artifact.reductions for p in r.site_params}
    pending = []
    for a, b in bounds:
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
                args.append(env[p] if p in env else getattr(_builtins, p))
        chunk_fn(*args)
        record = (a, b, slabs, sites)
        if commit_each:
            _commit([record], artifact, env, indices)
        else:
            pending.append(record)
    if pending:
        for plan in artifact.slabs:
            if plan.kind == "dict":
                overlap = audit_disjoint_dict_slabs([r[2][plan.param] for r in pending])
                assert overlap is None, f"write conflict on {overlap!r}"
        _commit(pending, artifact, env, indices)
    return env


def _commit(records, artifact, env, indices):
    for a, b, slabs, sites in records:
        for plan in artifact.slabs:
            container = resolve_path(env, plan.container)
            if plan.kind == "list":
                commit_list_slab(container, indices(a, b), slabs[plan.param])
            else:
                commit_dict_slab(container, slabs[plan.param])
        for red in artifact.reductions:
            current = resolve_path(env, red.scalar)
            current = fold_contributions(current, [sites[p] for p in red.site_params], red.op)
            assign_path(env, red.scalar, current)


def block(body_lines, header="for i in range(1, n):", clauses=""):
    suffix = f" {clauses}" if clauses else ""
    body = "\n".join("    " + line for line in body_lines)
    return f"# LUCEN START{suffix}\n{header}\n{body}\n# LUCEN END\n"


def test_basic_map_equivalence():
    src = block(["ys[i] = xs[i] * scale + 1"], header="for i in range(len(xs)):")
    env = {"xs": list(range(100)), "ys": [0] * 100, "scale": 3}
    assert run_parallel(src, env, chunks=4)["ys"] == golden(src, env)["ys"]


def test_conditional_write_preserves_originals():
    src = block(["if xs[i] % 2 == 0:", "    ys[i] = xs[i]"], header="for i in range(len(xs)):")
    env = {"xs": list(range(37)), "ys": [-1] * 37}
    assert run_parallel(src, env, chunks=5)["ys"] == golden(src, env)["ys"]


def test_local_temp_and_call():
    src = block(["tmp = transform(xs[i])", "ys[i] = tmp * 2"], header="for i in range(len(xs)):")
    env = {"xs": list(range(50)), "ys": [0] * 50, "transform": lambda v: v + 7}
    assert run_parallel(src, env)["ys"] == golden(src, env)["ys"]


def test_enumerate_domain():
    src = block(["out[idx] = item * item"], header="for idx, item in enumerate(items):")
    env = {"items": list(range(30)), "out": [0] * 30}
    assert run_parallel(src, env)["out"] == golden(src, env)["out"]


def test_sequence_domain_dict_fill_preserves_order():
    src = block(["cache[key] = key + suffix"], header="for key in keys:")
    env = {"keys": [f"k{n}" for n in range(40)], "cache": {}, "suffix": "!"}
    p, g = run_parallel(src, env, chunks=4), golden(src, env)
    assert list(p["cache"].items()) == list(g["cache"].items())


def test_float_reduction_is_bit_identical():
    src = block(["total += vals[i] * 1.000001"], header="for i in range(len(vals)):")
    env = {"vals": [0.1 * k + 0.013 for k in range(999)], "total": 0.25}
    assert run_parallel(src, env, chunks=7)["total"] == golden(src, env)["total"]


def test_min_reduction():
    src = block(["best = min(best, vals[i])"], header="for i in range(len(vals)):")
    env = {"vals": [((k * 37) % 101) - 50 for k in range(200)], "best": 10**9}
    assert run_parallel(src, env)["best"] == golden(src, env)["best"]


def test_conditional_reduction():
    src = block(["if vals[i] > 0:", "    total += vals[i]"], header="for i in range(len(vals)):")
    env = {"vals": [k - 50 for k in range(100)], "total": 0}
    assert run_parallel(src, env)["total"] == golden(src, env)["total"]


def test_attribute_scalar_reduction():
    class Acc:
        def __init__(self):
            self.total = 0.5

    src = block(["acc.total += vals[i]"], header="for i in range(len(vals)):")
    env = {"vals": [0.1 * k for k in range(300)], "acc": Acc()}
    assert run_parallel(src, env).get("acc").total == golden(src, env)["acc"].total


def test_aug_subscript_cell():
    src = block(["hist[i] += ws[i]", "hist[i] += 1"], header="for i in range(len(ws)):")
    env = {"hist": [5] * 60, "ws": list(range(60))}
    assert run_parallel(src, env, chunks=4)["hist"] == golden(src, env)["hist"]


def test_dag_block_level_order_equivalence():
    src = block(["out[i] = out[i // 2] + w[i]"], header="for i in range(1, n):")
    env = {"n": 64, "out": [1] + [0] * 63, "w": list(range(64))}
    levels = [(0, 1), (1, 3), (3, 7), (7, 15), (15, 31), (31, 63)]
    p = run_parallel(src, env, commit_each=True, bounds=levels)
    assert p["out"] == golden(src, env)["out"]


def test_asserted_permutation_writes():
    src = block(
        ["out[perm[i]] = i * 10"], header="for i in range(len(perm)):", clauses="depend=none"
    )
    perm = [(k * 7) % 20 for k in range(20)]
    env = {"perm": perm, "out": [-1] * 20}
    assert run_parallel(src, env, chunks=4)["out"] == golden(src, env)["out"]


def test_duplicate_keys_across_chunks_detected():
    src = block(["seen[key] = key"], header="for key in keys:")
    env = {"keys": ["a", "b", "c", "a", "d", "e"], "seen": {}}
    with pytest.raises(AssertionError, match="write conflict"):
        run_parallel(src, env, chunks=3)


def test_try_except_body():
    src = block(
        [
            "try:",
            "    out[i] = 100 // xs[i]",
            "except ZeroDivisionError:",
            "    out[i] = fallback",
        ],
        header="for i in range(len(xs)):",
    )
    env = {"xs": [1, 2, 0, 4, 0, 5, 10], "out": [0] * 7, "fallback": -9}
    assert run_parallel(src, env)["out"] == golden(src, env)["out"]


def test_chunk_function_has_zero_global_loads():
    src = block(["ys[i] = xs[i] * scale"], header="for i in range(len(xs)):")
    _, _, artifact = pipeline(src)
    chunk_fn, _ = artifact.compile_pair()
    banned = {"LOAD_GLOBAL", "LOAD_NAME", "LOAD_DEREF"}
    assert not [i.opname for i in dis.get_instructions(chunk_fn) if i.opname in banned]


def test_generated_sources_are_valid_python():
    src = block(["out[i] = combine(out[i // 2], w[i])"])
    _, _, artifact = pipeline(src)
    compile(artifact.source, "<chunk>", "exec")
    compile(artifact.seq_source, "<seq>", "exec")


def test_sequential_twin_roundtrip():
    src = block(["total += vals[i]"], header="for i in range(len(vals)):")
    _, _, artifact = pipeline(src)
    _, seq_fn = artifact.compile_pair()
    env = {"vals": [1, 2, 3, 4], "total": 10}
    args = []
    for p in artifact.seq_params:
        if p == "_plx_iter":
            args.append(range(len(env["vals"])))
        elif p == "_plx_skip":
            args.append(SKIP)
        else:
            args.append(env[p] if p in env else getattr(_builtins, p))
    result = seq_fn(*args)
    assert result == (3, 20)


def test_sequential_twin_zero_iterations():
    src = block(["ys[i] = xs[i]"], header="for i in range(len(xs)):")
    _, _, artifact = pipeline(src)
    _, seq_fn = artifact.compile_pair()
    args = [
        range(0) if p == "_plx_iter" else SKIP if p == "_plx_skip" else []
        for p in artifact.seq_params
    ]
    result = seq_fn(*args)
    assert result[0] is SKIP


def test_reduction_inside_nested_loop_routes_sequential():
    src = block(["while vals[i] > 0:", "    total += 1"], header="for i in range(len(vals)):")
    _, _, artifact = pipeline(src)
    assert artifact is None
    assert any(r.error == "UnmergeableConflictError" for r in get_fallback_report())


def test_sequential_blocks_generate_nothing():
    src = block(["out[i] = out[i - 1] + 1"])
    _, _, artifact = pipeline(src)
    assert artifact is None
