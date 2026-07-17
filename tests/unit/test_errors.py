from __future__ import annotations

import logging

import pytest

from lucen.support.errors import (
    ErrorsMode,
    PragmaStructureError,
    clear_fallback_report,
    get_fallback_report,
    raise_or_fallback,
    report_fallback,
    set_errors_mode,
)


@pytest.fixture(autouse=True)
def _reset():
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    yield
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()


def test_report_mode_records_and_logs_once_per_block(caplog):
    exc = PragmaStructureError("unmatched START", file="f.py", line=3)
    with caplog.at_level(logging.WARNING, logger="lucen"):
        raise_or_fallback(exc)
        raise_or_fallback(exc)
    assert len(get_fallback_report()) == 2
    logged = [r for r in caplog.records if "PragmaStructureError" in r.getMessage()]
    assert len(logged) == 1


def test_quiet_mode_records_without_logging(caplog):
    set_errors_mode("quiet")
    with caplog.at_level(logging.WARNING, logger="lucen"):
        raise_or_fallback(PragmaStructureError("x", file="f.py", line=1))
    assert len(get_fallback_report()) == 1
    assert caplog.records == []


def test_hard_mode_raises_and_records_nothing():
    set_errors_mode(ErrorsMode.HARD)
    with pytest.raises(PragmaStructureError):
        raise_or_fallback(PragmaStructureError("x", file="f.py", line=1))
    assert get_fallback_report() == ()


def test_routing_decisions_never_raise_even_in_hard_mode():
    set_errors_mode(ErrorsMode.HARD)
    rec = report_fallback(
        "predicted unprofitable", file="f.py", line=9, error="PARALLEL_UNPROFITABLE"
    )
    assert rec.error == "PARALLEL_UNPROFITABLE"
    assert len(get_fallback_report()) == 1


def test_invalid_mode_rejected():
    with pytest.raises(ValueError):
        set_errors_mode("loud")


def test_scanner_structural_error_raises_under_hard_mode():
    from lucen.analysis.scanner import scan_source

    set_errors_mode("hard")
    with pytest.raises(PragmaStructureError):
        scan_source("# LUCEN START\nfor i in r:\n    pass\n")
