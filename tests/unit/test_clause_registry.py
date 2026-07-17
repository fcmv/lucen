from __future__ import annotations

import pytest

from lucen.analysis.scanner import parse_clause_text
from lucen.clauses.registry import REGISTRY, validate_clause
from lucen.support.errors import ClauseValueError


def _cv(text: str):
    clauses = parse_clause_text(text)
    assert len(clauses) == 1
    return next(iter(clauses.items()))


def test_registry_has_sixteen_groups():
    keys = set(REGISTRY)
    assert "process_wait" not in keys
    assert "calibrate" in keys
    assert "trust" in keys
    assert len(keys - {"skip_runtime_check"}) == 16


def test_unknown_key_suggests_close_match():
    key, cv = _cv("backnd=thread")
    with pytest.raises(ClauseValueError) as exc_info:
        validate_clause("START", key, cv)
    assert "did you mean 'backend'" in str(exc_info.value)


def test_wrong_host_names_valid_host():
    key, cv = _cv("qualname=Cls.method")
    with pytest.raises(ClauseValueError) as exc_info:
        validate_clause("START", key, cv)
    assert "LUCEN TRUST" in str(exc_info.value)


def test_value_error_includes_accepted_forms():
    key, cv = _cv("nested=nope")
    with pytest.raises(ClauseValueError) as exc_info:
        validate_clause("START", key, cv)
    assert "shared_pool" in str(exc_info.value)


def test_allow_list_vocabulary_checked_with_hint():
    key, cv = _cv("strict=true(allow=[monotnic])")
    with pytest.raises(ClauseValueError) as exc_info:
        validate_clause("START", key, cv)
    assert "monotonic" in str(exc_info.value)


def test_bool_is_not_an_int():
    key, cv = _cv("backend=thread(pool_size=true)")
    with pytest.raises(ClauseValueError):
        validate_clause("START", key, cv)


def test_every_registry_entry_has_hosts_and_accepts():
    for key, spec in REGISTRY.items():
        assert spec.hosts <= {"START", "TRUST"}, key
        assert spec.accepts, key
