from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import PREFILTER_TOKEN, parse_clause_text, scan_source
from lucen.analysis.selector import select
from lucen.codegen import generate
from lucen.execution import dispatch
from lucen.support import config
from lucen.support.errors import (
    ClauseValueError,
    ErrorsMode,
    LucenError,
    clear_fallback_report,
    set_errors_mode,
)


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


_CLAUSE_CHARS = st.text(
    alphabet=st.characters(blacklist_characters="\n\r", blacklist_categories=("Cs",)), max_size=40
)

_TOKENS = st.sampled_from(
    [
        "backend=",
        "thread",
        "process",
        "sequential",
        "calibrate=",
        "false",
        "true",
        "chunks=",
        "8",
        "-3",
        "0",
        "timeout=",
        "5.0",
        "depend=",
        "none",
        "reduce=",
        "custom",
        "grainsize=",
        "trust=",
        "callables",
        "on_error=",
        "collect",
        "(",
        ")",
        ",",
        "=",
        " ",
        "1e9",
        "abc",
        "backend=thread(chunks=4)",
    ]
)
_CLAUSEY = st.lists(_TOKENS, max_size=12).map(lambda parts: "".join(parts))

CLAUSE_TEXT = st.one_of(_CLAUSE_CHARS, _CLAUSEY)


def _module_with_clause(clause):
    return f"# LUCEN START {clause}\nfor i in range(len(xs)):\n    ys[i] = xs[i] * 2\n# LUCEN END\n"


@given(clause=CLAUSE_TEXT)
def test_scanner_only_raises_clausevalueerror(clause):
    try:
        scan_source(_module_with_clause(clause), "fuzz.py")
    except ClauseValueError:
        pass
    except LucenError as exc:
        pytest.fail(f"unexpected {type(exc).__name__} for clause {clause!r}")
    except Exception as exc:  # noqa: BLE001 - an uncontrolled crash is the bug
        pytest.fail(f"uncontrolled {type(exc).__name__} for clause {clause!r}: {exc}")


@given(clause=CLAUSE_TEXT)
def test_parse_clause_text_is_controlled(clause):
    try:
        parse_clause_text(clause)
    except ClauseValueError:
        pass
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"uncontrolled {type(exc).__name__} for {clause!r}: {exc}")


_PRAGMA_LINES = st.sampled_from(
    [
        "# LUCEN START",
        "# LUCEN END",
        "# LUCEN TRUST",
        "# LUCEN START calibrate=false",
        "for i in range(len(xs)):",
        "    ys[i] = xs[i]",
        "def helper(x):",
        "    return x + 1",
        "x = 1",
        "",
    ]
)


@given(lines=st.lists(_PRAGMA_LINES, max_size=12))
def test_arbitrary_pragma_arrangements_do_not_crash(lines):
    src = "\n".join(lines) + "\n"
    if not _is_compilable(src):
        return
    try:
        scan_source(src, "fuzz.py")
    except ClauseValueError:
        pass
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"uncontrolled {type(exc).__name__}: {exc}\n{src}")


def _is_compilable(src):
    try:
        compile(src, "fuzz.py", "exec")
        return True
    except SyntaxError:
        return False


@given(src=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=300))
def test_prefilter_has_no_false_negatives(src):
    result = scan_source(src, "fuzz.py")
    if PREFILTER_TOKEN not in src:
        assert result.blocks == []
    if result.blocks:
        assert PREFILTER_TOKEN in src


@given(src=st.text(max_size=200))
def test_renaming_the_keyword_removes_all_blocks(src):
    neutered = src.replace("LUCEN", "PARALLEIGH")
    assert scan_source(neutered, "fuzz.py").blocks == []


_ATOM = st.sampled_from(["xs[i]", "i", "1", "2", "3"])


def _expr(depth):
    if depth <= 0:
        return _ATOM
    sub = _expr(depth - 1)
    return st.one_of(
        _ATOM,
        st.builds(lambda a, o, b: f"({a} {o} {b})", sub, st.sampled_from(["+", "-", "*"]), sub),
    )


_BODY = st.one_of(
    _expr(2).map(lambda e: (["ys[i] = " + e], "for i in range(len(xs)):")),
    _expr(2).map(lambda e: (["total += " + e], "for i in range(len(xs)):")),
    st.sampled_from(
        [
            (["ys[i] = ys[i - 1] + xs[i]"], "for i in range(1, len(xs)):"),
            (["out[i] = out[i // 2] + xs[i]"], "for i in range(1, len(xs)):"),
            (["ys[i % 3] = xs[i]"], "for i in range(len(xs)):"),
            (["break"], "for i in range(len(xs)):"),
        ]
    ),
)


@given(body_header=_BODY)
@settings(max_examples=120)
def test_pipeline_never_crashes_uncontrolled(body_header):
    body, header = body_header
    body_src = "\n".join("    " + line for line in body)
    src = f"# LUCEN START calibrate=false\n{header}\n{body_src}\n# LUCEN END\n"
    try:
        scan = scan_source(src, "fuzz.py")
        analyses = analyze_source(src, scan, "fuzz.py")
        for analysis in analyses:
            decision = select(analysis, workers=4)
            generate(analysis, decision, "fuzz.py")
    except LucenError:
        pass
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"uncontrolled {type(exc).__name__}: {exc}\n{src}")
