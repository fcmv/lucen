from __future__ import annotations

import json
import sys
import tokenize
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import BlockDecision, Eligibility, select
from lucen.support.errors import (
    ClauseValueError,
    ErrorsMode,
    LucenError,
    get_errors_mode,
    set_errors_mode,
)

BASELINE_FIELDS = ("parallel", "eligibility", "routed", "audit_tier", "dag_divisor", "unprofitable")


def interpreter_mode() -> str:
    probe = getattr(sys, "_is_gil_enabled", None)
    if probe is not None and not probe():
        return "free_threaded"
    return "gil"


@contextmanager
def _collected_errors():
    previous = get_errors_mode()
    set_errors_mode(ErrorsMode.QUIET)
    try:
        yield
    finally:
        set_errors_mode(previous)


def build_report(source: str, filename: str, assume: Optional[str] = None) -> Dict[str, Any]:
    mode = assume or interpreter_mode()
    with _collected_errors():
        scan = scan_source(source, filename)
        analyses = analyze_source(source, scan, filename)
        blocks = [_block_report(i, a, mode) for i, a in enumerate(analyses, 1)]
    return {
        "file": filename,
        "interpreter_mode": mode,
        "blocks": blocks,
        "file_fallbacks": [
            {"error": r.error, "line": r.line, "message": r.message} for r in scan.fallbacks
        ],
    }


def _block_report(index: int, analysis, mode: str) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "index": index,
        "line": analysis.block.start.lineno,
        "clauses": {k: cv.raw for k, cv in analysis.block.start.clauses.items()},
    }
    try:
        decision = select(analysis)
    except LucenError as exc:
        entry.update(
            {
                "parallel": False,
                "eligibility": "SEQUENTIAL",
                "routed": "SEQUENTIAL",
                "backend": "SEQUENTIAL",
                "audit_tier": None,
                "dag_divisor": None,
                "reduction_ops": {},
                "unprofitable": False,
                "fallbacks": [],
                "suggestion": None,
                "hard_error": type(exc).__name__,
                "reasons": [
                    f"strict= makes this a hard import-time error: "
                    f"{type(exc).__name__}: {exc.message}"
                ],
            }
        )
        return entry
    routed = decision.routed
    entry.update(
        {
            "parallel": routed is not Eligibility.SEQUENTIAL,
            "eligibility": decision.eligibility.name,
            "routed": routed.name,
            "backend": _backend_line(analysis, decision),
            "audit_tier": decision.audit_tier.name if decision.audit_tier else None,
            "dag_divisor": decision.dag_divisor,
            "reduction_ops": dict(decision.reduction_ops),
            "unprofitable": decision.unprofitable,
            "reasons": list(decision.reasons),
            "fallbacks": [{"error": r.error, "message": r.message} for r in decision.fallbacks],
            "suggestion": _suggestion(decision),
        }
    )
    return entry


def _backend_line(analysis, decision: BlockDecision) -> str:
    routed = decision.routed
    if routed is Eligibility.SEQUENTIAL:
        return "SEQUENTIAL"
    if routed is Eligibility.WAVEFRONT:
        return (
            "SEQUENTIAL by default (recognized-DAG wavefront; force "
            "backend=thread to parallelize its levels on a free-threaded build)"
        )
    picked = _predicted_backend(analysis, decision)
    if picked == "sequential":
        return "SEQUENTIAL (forced by backend=sequential clause)"
    scheduler = (
        "flat chunk scheduler"
        if routed is Eligibility.THREAD_CAPABLE
        else "chunk partials with chunk-order fold"
    )
    return f"{picked.upper()}, {scheduler}"


def _predicted_backend(analysis, decision: BlockDecision) -> str:
    from lucen.codegen import generate
    from lucen.execution.dispatch import _pick_backend, make_spec

    try:
        artifact = generate(analysis, decision, analysis.filename)
    except LucenError:
        artifact = None
    if artifact is None:
        return "sequential"
    return _pick_backend(make_spec(analysis, decision, artifact))


def _suggestion(decision: BlockDecision) -> Optional[Dict[str, str]]:
    if any(r.error == "UnresolvedDependencyShapeError" for r in decision.fallbacks):
        return {
            "text": "# LUCEN START depend=none",
            "clauses": "depend=none",
            "note": "asserts disjointness Lucen cannot verify itself; the "
            "runtime write-set audit still runs unless you also add "
            "skip_runtime_check=true (spec 7)",
        }
    if decision.unprofitable:
        return {
            "text": "# LUCEN START calibrate=false",
            "clauses": "calibrate=false",
            "note": "forces parallel dispatch despite the profitability estimate (spec 5.17)",
        }
    return None


def render_text(report: Dict[str, Any]) -> str:
    ok_mark, seq_mark = _marks()
    lines = [
        f"{report['file']}: {len(report['blocks'])} marked block(s) "
        f"[{report['interpreter_mode'].replace('_', '-')} interpreter assumed]"
    ]
    for b in report["blocks"]:
        lines.append("")
        lines.append(f"Block {b['index']} (line {b['line']})")
        if b["parallel"]:
            calibrated = str(b.get("clauses", {}).get("calibrate", "")).lower() != "false"
            header = f"  {ok_mark} Parallel-eligible" if calibrated else f"  {ok_mark} Parallelized"
            lines.append(header)
            lines.append(f"  Backend: {b['backend']}")
            if calibrated and "SEQUENTIAL" not in b["backend"]:
                lines.append(
                    "  Profitability: decided at runtime by the probe; a loop "
                    "with a light body may run SEQUENTIAL instead. Add "
                    "calibrate=false to force this backend."
                )
        else:
            header = f"  {seq_mark} Sequential"
            if b.get("unprofitable"):
                header += " (parallel-eligible; predicted unprofitable)"
            lines.append(header)
        for reason in b["reasons"]:
            lines.append(f"  Reason: {reason}")
        for fb in b.get("fallbacks", ()):
            if fb["message"] not in b["reasons"]:
                lines.append(f"  Fallback: {fb['error']}: {fb['message']}")
        if b.get("clauses"):
            rendered = ", ".join(f"{k}={v}" for k, v in b["clauses"].items())
            lines.append(f"  Clauses in effect: {rendered}")
        if b["parallel"]:
            lines.append(
                "  Runtime-dependent (never reported statically): argument "
                "picklability, custom-callable well-formedness, pool "
                "availability - see `lucen profile`."
            )
        suggestion = b.get("suggestion")
        if suggestion:
            lines.append("  Suggestion:")
            lines.append(f"    {suggestion['text']}")
            lines.append(f"    ({suggestion['note']})")
    for fb in report["file_fallbacks"]:
        lines.append("")
        lines.append(f"Dropped pragma at line {fb['line']}: {fb['error']}: {fb['message']}")
    return "\n".join(lines)


def compare_to_baseline(current: Dict[str, Any], baseline: Dict[str, Any]) -> List[str]:
    diffs: List[str] = []
    cur, base = current["blocks"], baseline.get("blocks", [])
    if len(cur) != len(base):
        diffs.append(f"block count changed: {len(base)} -> {len(cur)}")
    for i, (c, b) in enumerate(zip(cur, base), 1):
        for field_name in BASELINE_FIELDS:
            if c.get(field_name) != b.get(field_name):
                diffs.append(
                    f"block {i}: {field_name} changed: "
                    f"{b.get(field_name)!r} -> {c.get(field_name)!r}"
                )
    return diffs


def run(
    path: str,
    block: Optional[int] = None,
    fmt: str = "text",
    assume: Optional[str] = None,
    strict: bool = False,
    baseline_path: Optional[str] = None,
) -> int:
    try:
        with tokenize.open(path) as f:
            source = f.read()
    except OSError as exc:
        print(f"lucen: cannot read {path}: {exc}")
        return 1
    try:
        report = build_report(source, path, assume)
    except ClauseValueError as exc:
        print(f"error: {exc}")
        return 1

    if block is not None:
        selected = [b for b in report["blocks"] if b["index"] == block]
        if not selected:
            print(f"lucen: no block {block} in {path} ({len(report['blocks'])} block(s) found)")
            return 1
        report = {**report, "blocks": selected}

    if strict:
        if not baseline_path:
            print("lucen: --strict requires --baseline=<file>")
            return 2
        try:
            with open(baseline_path, encoding="utf-8") as f:
                baseline = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"lucen: cannot load baseline {baseline_path}: {exc}")
            return 2
        diffs = compare_to_baseline(report, baseline)
        if diffs:
            for diff in diffs:
                print(f"classification regression: {diff}")
            return 1
        print("baseline check: no classification changes")
        return 0

    if fmt == "json":
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))
    return 0


def _marks() -> "tuple[str, str]":
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "✓".encode(encoding)
        return "✓", "✗"
    except (UnicodeEncodeError, LookupError):
        return "+", "x"
