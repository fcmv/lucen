from __future__ import annotations

import ast
from typing import Dict, List, Optional, Set

from lucen.analysis.rewriter import BlockAnalysis, Classification
from lucen.analysis.scanner import Pragma, ScanResult
from lucen.support.errors import AmbiguousTrustedNameError, raise_or_fallback, report_fallback


def blocks_to_drop(
    scan: ScanResult, analyses: List[BlockAnalysis], tree: ast.Module, filename: str
) -> Set[int]:
    trusted = _trusted_defs(scan, tree)
    if not trusted:
        return set()
    collisions = {name for name, pragmas in trusted.items() if len(pragmas) > 1}
    dropped: Set[int] = set()
    for analysis in analyses:
        if not analysis.ok:
            continue
        line = analysis.block.start.lineno
        for call in _trusted_calls(analysis, set(trusted)):
            name = call.func.id
            if name in collisions and not _disambiguated(trusted[name]):
                raise_or_fallback(
                    AmbiguousTrustedNameError(
                        f"two trusted functions share the simple name '{name}'; "
                        "add qualname= to the TRUST pragmas (only this block is "
                        "affected)",
                        file=filename,
                        line=line,
                    )
                )
                dropped.add(line)
                break
            pragma = trusted[name][0]
            violation = _argument_violation(call, analysis, pragma)
            if violation is not None:
                report_fallback(
                    f"trusted call '{name}' receives shared state "
                    f"('{violation}'); add args=unchecked to its TRUST pragma "
                    "to assert this is safe (spec 5.3.4)",
                    file=filename,
                    line=line,
                    error="TrustedArgumentError",
                )
                dropped.add(line)
                break
    return dropped


def _trusted_defs(scan: ScanResult, tree: ast.Module) -> Dict[str, List[Pragma]]:
    defs = sorted(
        (
            (node.lineno, node.name)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ),
    )
    named: Dict[str, List[Pragma]] = {}
    for pragma in scan.trusted:
        for lineno, name in defs:
            if lineno > pragma.lineno:
                named.setdefault(name, []).append(pragma)
                break
    return named


def _disambiguated(pragmas: List[Pragma]) -> bool:
    return all("qualname" in p.clauses for p in pragmas)


def _trusted_calls(analysis: BlockAnalysis, names: Set[str]) -> List[ast.Call]:
    return [
        node
        for node in ast.walk(analysis.for_node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names
    ]


def _argument_violation(call: ast.Call, analysis: BlockAnalysis, pragma: Pragma) -> Optional[str]:
    args_cv = pragma.clauses.get("args")
    if args_cv is not None and args_cv.kind == "name" and args_cv.value == "unchecked":
        return None
    allowed_shared: Set[str] = set()
    if args_cv is not None and args_cv.kind == "call":
        only = args_cv.value.kwargs.get("only")
        if only is None:
            return None
        allowed_shared = {item.value for item in only.value}
    safe = (Classification.LOOP_LOCAL, Classification.OUTER_READONLY)
    domain = analysis.domain.names if analysis.domain else frozenset()
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        for node in ast.walk(arg):
            if not isinstance(node, ast.Name):
                continue
            name = node.id
            if name in domain or name in allowed_shared:
                continue
            info = analysis.targets.get(name)
            if info is not None and info.classification not in safe:
                return name
    return None
