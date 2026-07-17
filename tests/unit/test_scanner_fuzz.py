from __future__ import annotations

import pytest

from lucen.analysis.scanner import scan_source
from lucen.support.errors import ClauseValueError, clear_fallback_report


@pytest.fixture(autouse=True)
def _clean_report():
    clear_fallback_report()
    yield
    clear_fallback_report()


def start_block(clauses: str) -> str:
    return f"# LUCEN START {clauses}\nfor i in range(1, n):\n    out[i] = xs[i]\n# LUCEN END\n"


def trust_block(clauses: str) -> str:
    return f"# LUCEN TRUST {clauses}\ndef helper(x):\n    return x\n"


MALFORMED_START = [
    "backend=threed",
    "backend=1",
    "backend='thread'",
    "backend=thread(pool_size=0)",
    "backend=thread(pool_size=-2)",
    "backend=thread(pool_size=2.5)",
    "backend=thread(pool_size=true)",
    "backend=thread(pool_sizes=4)",
    "backend=thread(pool_size=1, pool_size=2)",
    "backend=process(pool_size=4)",
    "backend=process(pool=42)",
    "backend=sequential(x=1)",
    "calibrate=never",
    "calibrate=threshold()",
    "calibrate=threshold(min_gain=0)",
    "calibrate=threshold(min_gain=-1)",
    "calibrate=threshold(gain=2)",
    "nested=parallel",
    "nested=true",
    "depend=some",
    "depend=acyclic",
    "depend=acyclic()",
    "depend=acyclic(key=f)",
    "depend=none(x=1)",
    "skip_runtime_check=yes",
    "skip_runtime_check=1",
    "on_error=ignore",
    "on_error=collect(max_errors=-1)",
    "on_error=collect(max_errors=0)",
    "on_error=collect(max_errors=1.5)",
    "on_error=custom()",
    "on_error=custom(handler=5)",
    "strict=1",
    "strict=maybe",
    "strict=false(allow=[monotonic])",
    "strict=true(allow=[wrongreason])",
    "strict=true(allow=monotonic)",
    "strict=true()",
    "on_fallback=loud",
    "on_fallback=custom()",
    "on_fallback=hard(allow=[nope])",
    "on_fallback=hard()",
    "timeout=abc",
    "timeout=true",
    "timeout=-1",
    "timeout=0",
    "timeout=5(per_task=maybe)",
    "timeout=5(on_timeout=3)",
    "timeout=5(after=1)",
    "timeout=5(per_task=true)(x=1)",
    "reduction_order=fast",
    "reduction_order=custom()",
    "reduce=sum2",
    "reduce=custom(fn=f)",
    "reduce=custom(identity=0)",
    "reduce=custom(fn=f, identity=0, tree=5)",
    "reduce=custom(fn=f, identity=thread(x=1))",
    "grainsize=0",
    "grainsize=-1",
    "grainsize=2.5",
    "grainsize=64(min_workers=0)",
    "grainsize=64(workers=2)",
    "progress=yes",
    "progress=callback()",
    "progress=callback(cb, cb2)",
    "progress=callback(cb, per_task=1)",
    "affinity=spread",
    "affinity=explicit()",
    "affinity=explicit(cores=[1.5])",
    "affinity=explicit(cores=[-1])",
    "affinity=explicit(cores=[0], numa=-1)",
    "process_wait=true",
    "batch_size=4",
    "foo=1",
    "backnd=thread",
    "args=unchecked",
    "qualname=Cls.method",
    "grainsize=4, grainsize=8",
    "grainsize=",
    "=4",
    "grainsize 4",
    "grainsize=4 progress=true",
    "backend=thread(",
    "backend=thread)",
    "affinity=explicit(cores=[0,)",
    "on_error=collect(max_errors=)",
    "grainsize=4,, progress=true",
    "grainsize=4 @",
]


@pytest.mark.parametrize("clause", MALFORMED_START)
def test_malformed_start_clause_raises(clause):
    with pytest.raises(ClauseValueError):
        scan_source(start_block(clause), filename="bad.py")


MALFORMED_TRUST = [
    "args=maybe",
    "args=unchecked(only=names)",
    "args=unchecked(safe=[x])",
    "args=unchecked(only=[1])",
    "qualname=5",
    "backend=thread",
    "grainsize=4",
    "calibrate=false",
]


@pytest.mark.parametrize("clause", MALFORMED_TRUST)
def test_malformed_trust_clause_raises(clause):
    with pytest.raises(ClauseValueError):
        scan_source(trust_block(clause), filename="bad.py")


VALID_START = [
    "backend=thread",
    "backend=process",
    "backend=sequential",
    "backend=thread(pool_size=1)",
    "backend=thread(chunks=0)",
    "backend=thread(pool_size=4, chunks=2)",
    "backend=process(chunks=3)",
    "backend=process(pool=mk.pool)",
    "calibrate=true",
    "calibrate=false",
    "calibrate=static",
    "calibrate=always",
    "calibrate=threshold(min_gain=2.5)",
    "calibrate=threshold(min_gain=1)",
    "nested=sequential",
    "nested=shared_pool",
    "nested=independent",
    "depend=none",
    "depend=acyclic(order=mod.key)",
    "skip_runtime_check=true",
    "skip_runtime_check=false",
    "on_error=collect",
    "on_error=collect(max_errors=1)",
    "on_error=custom(handler=hooks.on_bad)",
    "strict=true",
    "strict=false",
    "strict=true(allow=[monotonic])",
    "strict=true(allow=[unprofitable, early_exit])",
    "on_fallback=hard",
    "on_fallback=quiet",
    "on_fallback=report",
    "on_fallback=hard(allow=[modular])",
    "on_fallback=quiet(allow=[nested, branch_merge])",
    "on_fallback=custom(handler=h)",
    "timeout=1",
    "timeout=0.5",
    "timeout=5(per_task=true)",
    "timeout=5(per_task=false, on_timeout=t.handle)",
    "reduction_order=sequential_equivalent",
    "reduction_order=stable",
    "reduction_order=custom(combine=c)",
    "reduce=sum",
    "reduce=prod",
    "reduce=min",
    "reduce=max",
    "reduce=count",
    "reduce=bit_and",
    "reduce=concat",
    "reduce=custom(fn=f, identity=0)",
    "reduce=custom(fn=m.f, identity='x', tree=true)",
    "grainsize=1",
    "grainsize=7(min_workers=1)",
    "progress=true",
    "progress=false",
    "progress=callback(cb)",
    "progress=callback(m.cb, per_task=True, include_result=False)",
    "affinity=compact",
    "affinity=scatter",
    "affinity=explicit(cores=[])",
    "affinity=explicit(cores=[0, 1], numa_node=2)",
    "grainsize=64, progress=true, calibrate=false",
]


@pytest.mark.parametrize("clause", VALID_START)
def test_valid_start_clause_accepted(clause):
    result = scan_source(start_block(clause), filename="ok.py")
    assert len(result.blocks) == 1
    assert result.fallbacks == []


VALID_TRUST = [
    "args=checked",
    "args=unchecked",
    "args=unchecked(only=[])",
    "args=unchecked(only=[shared, cache], skip_runtime_check=false)",
    "qualname=Worker.transform",
    "qualname=Worker.transform(module=pkg.helpers)",
    "qualname=registry_key",
]


@pytest.mark.parametrize("clause", VALID_TRUST)
def test_valid_trust_clause_accepted(clause):
    result = scan_source(trust_block(clause), filename="ok.py")
    assert len(result.trusted) == 1
    assert result.fallbacks == []


ORDINARY_COMMENTS = [
    "# PARALLEL\n",
    "# PARALLELSTART\n",
    "# parallel start\n",
    "# Parallel Start\n",
    "# PARALLEL beginning of section\n",
    "## LUCEN START\n",
    "#: LUCEN START\n",
    "# LUCEN STARTED early\n",
    "# LUCEN ENDS here\n",
    "# THE LUCEN START marker\n",
    "s = '# LUCEN START bogus('\n",
    "b = b'# LUCEN END'\n",
    '"""\n# LUCEN START backend=junk(\n"""\n',
    "print('LUCEN START')\n",
]


@pytest.mark.parametrize("source", ORDINARY_COMMENTS)
def test_non_pragma_text_is_just_a_comment(source):
    result = scan_source(source + "x = 1\n")
    assert result.pragmas == []
    assert result.fallbacks == []


def test_crlf_line_endings():
    src = (
        "# LUCEN START grainsize=4\r\n"
        "for i in range(1, n):\r\n"
        "    out[i] = xs[i]\r\n"
        "# LUCEN END\r\n"
    )
    result = scan_source(src)
    assert len(result.blocks) == 1
    assert result.blocks[0].start.clauses["grainsize"].value == 4


def test_pragma_last_line_no_trailing_newline():
    src = "# LUCEN START\nfor i in range(2):\n    pass\n# LUCEN END"
    assert len(scan_source(src).blocks) == 1


def test_tab_indented_block_inside_function():
    src = (
        "def f(xs, out, n):\n"
        "\t# LUCEN START\n"
        "\tfor i in range(1, n):\n"
        "\t\tout[i] = xs[i]\n"
        "\t# LUCEN END\n"
    )
    assert len(scan_source(src).blocks) == 1


def test_trust_at_end_of_file_falls_back():
    result = scan_source("x = 1\n# LUCEN TRUST\n")
    assert result.trusted == []
    assert result.fallbacks[0].error == "TrustPragmaScopeError"


def test_trust_before_decorator_falls_back():
    src = "# LUCEN TRUST\n@decorator\ndef f():\n    pass\n"
    result = scan_source(src)
    assert result.trusted == []
    assert result.fallbacks[0].error == "TrustPragmaScopeError"


def test_stacked_trust_pragmas_both_attach():
    src = "# LUCEN TRUST\n# LUCEN TRUST args=unchecked\ndef f():\n    pass\n"
    result = scan_source(src)
    assert len(result.trusted) == 2


def test_empty_clause_list_is_fine():
    src = "# LUCEN START \nfor i in range(2):\n    pass\n# LUCEN END\n"
    result = scan_source(src)
    assert result.blocks[0].start.clauses == {}


def test_string_value_containing_comma_and_paren():
    src = start_block("reduce=custom(fn=f, identity='a,b)(')")
    result = scan_source(src)
    clause = result.blocks[0].start.clauses["reduce"]
    assert clause.value.kwargs["identity"].value == "a,b)("
