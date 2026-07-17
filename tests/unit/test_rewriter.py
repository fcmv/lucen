from __future__ import annotations

import pytest

from lucen.analysis.rewriter import AuditTier, Classification, analyze_source
from lucen.analysis.scanner import scan_source
from lucen.support.errors import (
    ErrorsMode,
    IllegalSyntaxInBlockError,
    clear_fallback_report,
    set_errors_mode,
)


@pytest.fixture(autouse=True)
def _clean_state():
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    yield
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()


def analyze_one(src: str):
    scan = scan_source(src, "t.py")
    analyses = analyze_source(src, scan, "t.py")
    assert len(analyses) == 1
    return analyses[0]


def block(body_lines, header="for i in range(1, n):"):
    body = "\n".join("    " + line for line in body_lines)
    return f"# LUCEN START\n{header}\n{body}\n# LUCEN END\n"


def cls(analysis, name):
    return analysis.targets[name].classification


def test_recognized_dag_example_spec_9_3():
    a = analyze_one(block(["results[i] = combine(results[i // 2], weights[i])"]))
    assert a.ok
    results = a.targets["results"]
    assert results.classification is Classification.READ_AFTER_WRITE
    assert len(results.write_indexes) == 1
    assert len(results.read_indexes) == 1
    assert a.targets["weights"].classification is Classification.OUTER_READONLY
    assert a.targets["combine"].classification is Classification.OUTER_READONLY
    assert "i" not in a.targets
    assert set(a.domain.proven) == {"i"}


def test_basic_map_with_local():
    a = analyze_one(
        block(
            ["tmp = transform(items[i])", "results[i] = tmp * scale"],
            header="for i in range(len(items)):",
        )
    )
    assert a.ok
    results = a.targets["results"]
    assert results.classification is Classification.SHARED_INDEXED_SAFE
    assert results.audit_tier is AuditTier.BY_PROOF
    assert a.targets["tmp"].classification is Classification.LOOP_LOCAL
    for name in ("transform", "items", "scale", "len"):
        assert a.targets[name].classification is Classification.OUTER_READONLY


def test_enumerate_index_is_proven():
    a = analyze_one(block(["results[idx] = item * 2"], header="for idx, item in enumerate(items):"))
    assert a.ok
    assert a.targets["results"].audit_tier is AuditTier.BY_PROOF
    assert set(a.domain.proven) == {"idx"}
    assert set(a.domain.values) == {"item"}


def test_loop_value_key_is_by_assumption():
    a = analyze_one(block(["cache[key] = expensive(key)"], header="for key in keys:"))
    assert a.ok
    cache = a.targets["cache"]
    assert cache.classification is Classification.SHARED_INDEXED_SAFE
    assert cache.audit_tier is AuditTier.BY_ASSUMPTION


def test_attribute_write_through_loop_value():
    a = analyze_one(block(["obj.result = compute(obj)"], header="for obj in objects:"))
    assert a.ok
    info = a.targets["obj.result"]
    assert info.classification is Classification.SHARED_INDEXED_SAFE
    assert info.audit_tier is AuditTier.BY_ASSUMPTION


def test_sum_reduction():
    a = analyze_one(block(["total += weights[i]"]))
    assert a.ok
    total = a.targets["total"]
    assert total.classification is Classification.SHARED_SCALAR
    assert total.reduce_op == "+"


def test_conditional_reduction_untouched_else():
    a = analyze_one(block(["if weights[i] > 0:", "    total += weights[i]"]))
    assert a.ok
    assert a.targets["total"].reduce_op == "+"


def test_min_self_assignment():
    a = analyze_one(block(["best = min(best, values[i])"]))
    assert a.ok
    best = a.targets["best"]
    assert best.classification is Classification.SHARED_SCALAR
    assert best.reduce_op == "min"


def test_plain_conditional_overwrite_has_no_op():
    a = analyze_one(block(["if flags[i]:", "    winner = values[i]"]))
    assert a.ok
    winner = a.targets["winner"]
    assert winner.classification is Classification.SHARED_SCALAR
    assert winner.reduce_op is None


def test_permuted_index_is_unresolved():
    a = analyze_one(block(["results[perm[i]] = values[i]"]))
    assert a.ok
    assert a.targets["results"].classification is Classification.SHARED_INDEXED_UNRESOLVED
    assert a.targets["perm"].classification is Classification.OUTER_READONLY


def test_offset_index_is_unresolved_here():
    a = analyze_one(block(["results[i + 1] = values[i]"]))
    assert a.targets["results"].classification is Classification.SHARED_INDEXED_UNRESOLVED


def test_aug_subscript_reads_then_writes():
    a = analyze_one(block(["hist[i] += 1"]))
    hist = a.targets["hist"]
    assert hist.classification is Classification.READ_AFTER_WRITE
    assert len(hist.write_indexes) == 1
    assert len(hist.read_indexes) == 1


def test_element_method_mutation_is_self_indexed():
    a = analyze_one(block(["rows[i].append(values[i])"]))
    assert a.targets["rows"].classification is Classification.READ_AFTER_WRITE


def test_container_method_mutation_is_unresolved():
    a = analyze_one(block(["results.append(items[i])"], header="for i in range(len(items)):"))
    assert a.targets["results"].classification is Classification.SHARED_INDEXED_UNRESOLVED


def test_attribute_scalar_reduction():
    a = analyze_one(block(["acc.total += values[i]"]))
    info = a.targets["acc.total"]
    assert info.classification is Classification.SHARED_SCALAR
    assert info.reduce_op == "+"


def test_reassigned_loop_variable_loses_proof():
    a = analyze_one(block(["i = i + 1", "results[i] = values[i]"]))
    assert a.targets["results"].classification is Classification.SHARED_INDEXED_UNRESOLVED


def test_comprehension_targets_do_not_leak():
    a = analyze_one(block(["results[i] = [y * 2 for y in rows[i]]"]))
    assert a.ok
    assert "y" not in a.targets
    assert a.targets["rows"].classification is Classification.OUTER_READONLY


def test_branch_conflict_falls_back():
    a = analyze_one(
        block(
            [
                "if flags[i]:",
                "    results[i] = 1",
                "else:",
                "    results[i - 1] = 2",
            ]
        )
    )
    assert not a.ok
    assert a.fallbacks[0].error == "BranchMergeConflictError"


def test_elif_chain_is_single_level():
    a = analyze_one(
        block(
            [
                "if kinds[i] == 1:",
                "    results[i] = 1",
                "elif kinds[i] == 2:",
                "    results[i] = 2",
                "else:",
                "    results[i] = 3",
            ]
        )
    )
    assert a.ok
    assert a.targets["results"].audit_tier is AuditTier.BY_PROOF


def test_condition_may_not_reference_shared_state():
    a = analyze_one(block(["total += values[i]", "if total > 100:", "    pass"]))
    assert not a.ok
    assert a.fallbacks[0].error == "IllegalSyntaxInBlockError"


def test_condition_on_loop_var_and_readonly_ok():
    a = analyze_one(block(["if i % 2 == 0:", "    results[i] = values[i]"]))
    assert a.ok


@pytest.mark.parametrize(
    "body",
    [
        ["with open(paths[i]) as f:", "    results[i] = f.read()"],
        ["global total", "total = 1"],
        ["def helper():", "    return 1"],
        ["class C:", "    pass"],
        ["yield results[i]"],
    ],
)
def test_illegal_constructs_fall_back(body):
    a = analyze_one(block(body))
    assert not a.ok
    assert a.fallbacks[0].error == "IllegalSyntaxInBlockError"


def test_region_must_be_exactly_one_for_loop():
    src = "# LUCEN START\nx = 1\nfor i in range(3):\n    pass\n# LUCEN END\n"
    a = analyze_one(src)
    assert not a.ok


def test_for_else_is_illegal():
    src = "# LUCEN START\nfor i in range(3):\n    pass\nelse:\n    pass\n# LUCEN END\n"
    a = analyze_one(src)
    assert not a.ok


def test_break_is_flagged_not_fatal():
    a = analyze_one(
        block(
            ["if items[i] < 0:", "    break", "results[i] = items[i]"],
            header="for i in range(len(items)):",
        )
    )
    assert a.ok
    assert a.has_break


def test_return_inside_function_block():
    src = (
        "def run(items, results):\n"
        "    # LUCEN START\n"
        "    for i in range(len(items)):\n"
        "        if items[i] < 0:\n"
        "            return None\n"
        "        results[i] = items[i]\n"
        "    # LUCEN END\n"
    )
    a = analyze_one(src)
    assert a.ok
    assert a.has_return


def test_try_except_arms_are_single_level():
    a = analyze_one(
        block(
            [
                "try:",
                "    results[i] = parse(raw[i])",
                "except ValueError:",
                "    results[i] = default",
            ]
        )
    )
    assert a.ok
    assert a.targets["results"].audit_tier is AuditTier.BY_PROOF


def test_multiple_blocks_analyzed_independently():
    src = (
        "# LUCEN START\nfor i in range(n):\n    out[i] = f(i)\n# LUCEN END\n"
        "# LUCEN START\nfor j in range(n):\n    with lock:\n        pass\n# LUCEN END\n"
    )
    scan = scan_source(src, "t.py")
    analyses = analyze_source(src, scan, "t.py")
    assert len(analyses) == 2
    assert analyses[0].ok
    assert not analyses[1].ok


def test_hard_mode_raises_instead_of_falling_back():
    set_errors_mode(ErrorsMode.HARD)
    src = block(["with lock:", "    pass"])
    scan_result = scan_source(src, "t.py")
    with pytest.raises(IllegalSyntaxInBlockError):
        analyze_source(src, scan_result, "t.py")


def test_nested_for_is_now_supported():
    a = analyze_one(
        block(
            ["s = 0", "for v in rows[i]:", "    s += v", "out[i] = s"],
            header="for i in range(len(rows)):",
        )
    )
    assert a.ok
    assert cls(a, "out") is Classification.SHARED_INDEXED_SAFE
    assert cls(a, "s") is Classification.LOOP_LOCAL


def test_nested_if_in_while_is_now_supported():
    a = analyze_one(
        block(
            [
                "n = xs[i]",
                "steps = 0",
                "while n > 1:",
                "    if n % 2 == 0:",
                "        n = n // 2",
                "    else:",
                "        n = 3 * n + 1",
                "    steps += 1",
                "out[i] = steps",
            ],
            header="for i in range(len(xs)):",
        )
    )
    assert a.ok
    assert cls(a, "out") is Classification.SHARED_INDEXED_SAFE


def test_inner_loop_break_is_not_outer_early_exit():
    a = analyze_one(
        block(
            [
                "found = -1",
                "for j in range(len(rows[i])):",
                "    if rows[i][j] < 0:",
                "        found = j",
                "        break",
                "out[i] = found",
            ],
            header="for i in range(len(rows)):",
        )
    )
    assert a.ok
    assert not a.has_break
