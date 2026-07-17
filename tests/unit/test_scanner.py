from __future__ import annotations

import pytest

from lucen.analysis.scanner import parse_clause_text, scan_source
from lucen.support.errors import ClauseValueError, clear_fallback_report


@pytest.fixture(autouse=True)
def _clean_report():
    clear_fallback_report()
    yield
    clear_fallback_report()


def test_no_pragmas_is_empty_and_silent():
    src = "x = 1\nfor i in range(10):\n    x += i\n# a normal comment\n"
    result = scan_source(src)
    assert result.pragmas == []
    assert result.blocks == []
    assert result.fallbacks == []


def test_pragma_inside_string_literal_is_ignored():
    src = 's = "# LUCEN START backend=totally_bogus("\n'
    result = scan_source(src)
    assert result.pragmas == []


def test_case_sensitivity_and_word_boundaries():
    src = "# parallel start\n# LUCEN STARTS ARE FUN\n# PARALLELISM IS GREAT\nx = 1\n"
    result = scan_source(src)
    assert result.pragmas == []
    assert result.fallbacks == []


def test_unparseable_file_returns_empty():
    src = "def broken(:\n# LUCEN START\n"
    result = scan_source(src)
    assert result.pragmas == []


BASIC = """\
import math

# LUCEN START grainsize=64, progress=true
for i in range(1, n):
    results[i] = combine(results[i // 2], weights[i])
# LUCEN END
"""


def test_basic_block_found_with_clauses():
    result = scan_source(BASIC, filename="myfile.py")
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.start.lineno == 3
    assert block.end.lineno == 6
    clauses = block.start.clauses
    assert clauses["grainsize"].value == 64
    assert clauses["progress"].value is True
    assert result.fallbacks == []


def test_whitespace_tolerance():
    src = "#   LUCEN START\nfor i in r:\n    pass\n#LUCEN END\n"
    result = scan_source(src)
    assert len(result.blocks) == 1


def test_trust_before_def_ok():
    src = "# LUCEN TRUST\ndef transform(item):\n    return item * 2\n"
    result = scan_source(src)
    assert len(result.trusted) == 1
    assert result.fallbacks == []


def test_trust_with_blank_and_comment_lines_before_def():
    src = "# LUCEN TRUST\n\n# helper below\nasync def f(x):\n    return x\n"
    result = scan_source(src)
    assert len(result.trusted) == 1


def test_trust_not_before_def_falls_back():
    src = "# LUCEN TRUST\nx = 1\n"
    result = scan_source(src)
    assert result.trusted == []
    assert len(result.fallbacks) == 1
    assert result.fallbacks[0].error == "TrustPragmaScopeError"


def test_unmatched_start_falls_back():
    src = "# LUCEN START\nfor i in r:\n    pass\n"
    result = scan_source(src)
    assert result.blocks == []
    assert len(result.fallbacks) == 1
    assert result.fallbacks[0].error == "PragmaStructureError"


def test_end_without_start_falls_back():
    src = "x = 1\n# LUCEN END\n"
    result = scan_source(src)
    assert result.blocks == []
    assert result.fallbacks[0].error == "PragmaStructureError"


def test_nested_start_drops_inner_keeps_outer():
    src = "# LUCEN START\nfor i in r:\n    pass\n# LUCEN START\n# LUCEN END\n"
    result = scan_source(src)
    assert len(result.blocks) == 1
    assert result.blocks[0].start.lineno == 1
    assert len(result.fallbacks) == 1


def test_trailing_pragma_on_code_line_falls_back():
    src = "for i in r:  # LUCEN START\n    pass\n"
    result = scan_source(src)
    assert result.blocks == []
    assert any(r.error == "PragmaSyntaxError" for r in result.fallbacks)


def test_two_sequential_blocks():
    src = (
        "# LUCEN START\nfor i in a:\n    pass\n# LUCEN END\n"
        "x = 1\n"
        "# LUCEN START\nfor j in b:\n    pass\n# LUCEN END\n"
    )
    result = scan_source(src)
    assert len(result.blocks) == 2


@pytest.mark.parametrize(
    "clause_text",
    [
        "backend=threed",
        "foo=1",
        "backend=thread(pool_sze=16)",
        "backend=thread, backend=process",
        "grainsize=0",
        "grainsize=-3",
        "strict=maybe",
        "timeout=-1",
        "timeout=0",
        "args=unchecked",
        "process_wait=true",
        "on_error=collect(max_errors=0)",
        "affinity=explicit(cores=[0, x])",
        "progress=callback()",
        "reduce=custom(fn=f)",
        "depend=acyclic()",
        "calibrate=sometimes",
        "backend=",
        "backend",
        "grainsize=64 progress=true",
        "backend=thread(pool_size=16",
    ],
)
def test_malformed_clauses_raise(clause_text):
    src = f"# LUCEN START {clause_text}\nfor i in r:\n    pass\n# LUCEN END\n"
    with pytest.raises(ClauseValueError):
        scan_source(src, filename="bad.py")


def test_end_takes_no_clauses():
    src = "# LUCEN START\nfor i in r:\n    pass\n# LUCEN END grainsize=4\n"
    with pytest.raises(ClauseValueError):
        scan_source(src)


def test_error_message_has_location_and_hint():
    src = "\n\n# LUCEN START backend=threed\nfor i in r:\n    pass\n# LUCEN END\n"
    with pytest.raises(ClauseValueError) as exc_info:
        scan_source(src, filename="myfile.py")
    msg = str(exc_info.value)
    assert "myfile.py:3" in msg
    assert "thread" in msg


def test_removed_clause_names_its_replacement():
    src = "# LUCEN START process_wait=true\nfor i in r:\n    pass\n# LUCEN END\n"
    with pytest.raises(ClauseValueError) as exc_info:
        scan_source(src)
    msg = str(exc_info.value)
    assert "removed" in msg and "§5.8" in msg


@pytest.mark.parametrize(
    "clause_text",
    [
        "backend=thread",
        "backend=sequential",
        "backend=thread(pool_size=16, chunks=8)",
        "backend=process(chunks=4, pool=my_mod.make_pool)",
        "calibrate=false",
        "calibrate=static",
        "calibrate=threshold(min_gain=1.5)",
        "nested=shared_pool",
        "depend=none",
        "depend=none, skip_runtime_check=true",
        "depend=acyclic(order=my_mod.order_key)",
        "on_error=collect",
        "on_error=collect(max_errors=3)",
        "on_error=custom(handler=my_handler)",
        "strict=true",
        "strict=true(allow=[monotonic, unprofitable])",
        "on_fallback=hard(allow=[monotonic])",
        "on_fallback=custom(handler=cb)",
        "timeout=2.5",
        "timeout=30(per_task=true, on_timeout=handle_it)",
        "reduction_order=sequential_equivalent",
        "reduction_order=custom(combine=my_combine)",
        "reduce=sum",
        "reduce=custom(fn=my_mod.combine, identity=0)",
        "reduce=custom(fn=f, identity='', tree=false)",
        "grainsize=1024",
        "grainsize=64(min_workers=8)",
        "progress=true",
        "progress=callback(my_cb)",
        "progress=callback(my_cb, per_task=True, include_result=True)",
        "affinity=compact",
        "affinity=explicit(cores=[0, 2, 4], numa_node=0)",
    ],
)
def test_valid_start_clauses(clause_text):
    src = f"# LUCEN START {clause_text}\nfor i in r:\n    pass\n# LUCEN END\n"
    result = scan_source(src)
    assert len(result.blocks) == 1
    assert result.fallbacks == []


@pytest.mark.parametrize(
    "clause_text",
    [
        "args=unchecked",
        "args=checked",
        "args=unchecked(only=[shared_list], skip_runtime_check=true)",
        "qualname=Worker.transform",
        "qualname=Worker.transform(module=pkg.helpers)",
    ],
)
def test_valid_trust_clauses(clause_text):
    src = f"# LUCEN TRUST {clause_text}\ndef f(x):\n    return x\n"
    result = scan_source(src)
    assert len(result.trusted) == 1
    assert result.fallbacks == []


def test_parse_clause_text_shapes():
    clauses = parse_clause_text("backend=thread(pool_size=16), grainsize=64")
    assert set(clauses) == {"backend", "grainsize"}
    backend = clauses["backend"]
    assert backend.kind == "call"
    assert backend.value.base.value == "thread"
    assert backend.value.kwargs["pool_size"].value == 16
    assert backend.raw == "thread(pool_size=16)"
    assert clauses["grainsize"].value == 64


def test_parse_clause_text_list_and_positional():
    clauses = parse_clause_text("affinity=explicit(cores=[0, 2, 4]), progress=callback(cb)")
    cores = clauses["affinity"].value.kwargs["cores"]
    assert cores.kind == "list"
    assert [c.value for c in cores.value] == [0, 2, 4]
    assert clauses["progress"].value.args[0].value == "cb"


def test_parse_clause_text_empty():
    assert parse_clause_text("") == {}
