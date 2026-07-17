from __future__ import annotations

import json

import pytest

from lucen.analysis.scanner import parse_clause_text
from lucen.cli import main
from lucen.cli.explain import build_report, compare_to_baseline, render_text
from lucen.support.errors import (
    ErrorsMode,
    clear_fallback_report,
    get_errors_mode,
    set_errors_mode,
)

DAG_SRC = (
    "# LUCEN START grainsize=64, progress=true\n"
    "for i in range(1, n):\n"
    "    results[i] = combine(results[i // 2], weights[i])\n"
    "# LUCEN END\n"
)

UNRESOLVED_SRC = (
    "# LUCEN START\nfor i in range(1, n):\n    results[perm[i]] = values[i]\n# LUCEN END\n"
)

TINY_SRC = "# LUCEN START\nfor i in range(200):\n    ys[i] = xs[i] * 2\n# LUCEN END\n"


@pytest.fixture(autouse=True)
def _clean_state():
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    yield
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()


def test_dag_block_reported_wavefront_sequential_default():
    report = build_report(DAG_SRC, "f.py", assume="free_threaded")
    (b,) = report["blocks"]
    assert b["eligibility"] == "WAVEFRONT"
    assert b["dag_divisor"] == 2
    assert "SEQUENTIAL by default" in b["backend"]
    assert "backend=thread" in b["backend"]
    assert b["clauses"] == {"grainsize": "64", "progress": "true"}


def test_backend_choice_is_interpreter_independent():
    free = build_report(DAG_SRC, "f.py", assume="free_threaded")["blocks"][0]
    gil = build_report(DAG_SRC, "f.py", assume="gil")["blocks"][0]
    assert free["eligibility"] == gil["eligibility"] == "WAVEFRONT"
    assert free["backend"] == gil["backend"]


def test_unresolved_block_gets_pasteable_suggestion():
    report = build_report(UNRESOLVED_SRC, "f.py", assume="free_threaded")
    (b,) = report["blocks"]
    assert not b["parallel"]
    suggestion = b["suggestion"]
    assert suggestion["text"].startswith("# LUCEN START ")
    parsed = parse_clause_text(suggestion["clauses"])
    assert "depend" in parsed


def test_unprofitable_block_suggests_calibrate_false():
    report = build_report(TINY_SRC, "f.py", assume="free_threaded")
    (b,) = report["blocks"]
    assert b["unprofitable"]
    assert not b["parallel"]
    assert b["eligibility"] == "THREAD_CAPABLE"
    parsed = parse_clause_text(b["suggestion"]["clauses"])
    assert "calibrate" in parsed


def test_strict_clause_reported_not_raised():
    src = UNRESOLVED_SRC.replace("# LUCEN START", "# LUCEN START strict=true")
    report = build_report(src, "f.py", assume="free_threaded")
    (b,) = report["blocks"]
    assert b["hard_error"] == "UnresolvedDependencyShapeError"
    assert not b["parallel"]


def test_explain_collects_quietly_and_restores_mode():
    set_errors_mode(ErrorsMode.HARD)
    report = build_report(UNRESOLVED_SRC, "f.py", assume="gil")
    assert len(report["blocks"]) == 1
    assert get_errors_mode() is ErrorsMode.HARD


def test_render_text_is_stable():
    text = render_text(build_report(DAG_SRC, "f.py", assume="free_threaded"))
    assert "Block 1 (line 1)" in text
    assert "Runtime-dependent" in text
    assert "grainsize=64" in text


def test_report_is_json_serializable():
    report = build_report(DAG_SRC, "f.py", assume="gil")
    assert json.loads(json.dumps(report)) == report


def test_baseline_no_false_positive_on_unrelated_edit():
    baseline = build_report(DAG_SRC, "f.py", assume="gil")
    edited = "# an unrelated comment\nx = 1\n" + DAG_SRC
    current = build_report(edited, "f.py", assume="gil")
    assert compare_to_baseline(current, baseline) == []


def test_baseline_catches_classification_regression():
    baseline = build_report(DAG_SRC, "f.py", assume="gil")
    regressed = DAG_SRC.replace("results[i // 2]", "results[perm[i]]")
    current = build_report(regressed, "f.py", assume="gil")
    diffs = compare_to_baseline(current, baseline)
    assert any("parallel" in d for d in diffs)


def test_cli_end_to_end(tmp_path, capsys):
    path = tmp_path / "sample.py"
    path.write_text(DAG_SRC, encoding="utf-8")
    assert main(["explain", str(path), "--assume-free-threaded"]) == 0
    out = capsys.readouterr().out
    assert "Block 1" in out

    assert main(["explain", str(path), "--format", "json", "--block", "1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocks"][0]["index"] == 1

    assert main(["explain", str(path), "--block", "9"]) == 1
    capsys.readouterr()


def test_cli_strict_baseline_roundtrip(tmp_path, capsys):
    path = tmp_path / "sample.py"
    path.write_text(DAG_SRC, encoding="utf-8")
    assert main(["explain", str(path), "--format", "json", "--assume-gil"]) == 0
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(capsys.readouterr().out, encoding="utf-8")

    assert (
        main(["explain", str(path), "--assume-gil", "--strict", "--baseline", str(baseline_file)])
        == 0
    )
    capsys.readouterr()

    path.write_text(DAG_SRC.replace("results[i // 2]", "results[perm[i]]"), encoding="utf-8")
    assert (
        main(["explain", str(path), "--assume-gil", "--strict", "--baseline", str(baseline_file)])
        == 1
    )
    assert "regression" in capsys.readouterr().out
