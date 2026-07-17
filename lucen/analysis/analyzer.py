from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, List, Optional

from lucen.analysis.rewriter import BlockAnalysis, Classification, TargetInfo


class DependencyShape(Enum):
    SELF_CONTAINED = auto()
    MONOTONIC_OFFSET = auto()
    RECOGNIZED_DAG = auto()
    MODULAR_SELF_REFERENCE = auto()
    UNRESOLVED = auto()


@dataclass(frozen=True)
class ShapeResult:
    shape: DependencyShape
    divisor: Optional[int] = None
    offset: Optional[int] = None


def resolve_shapes(analysis: BlockAnalysis) -> Dict[str, ShapeResult]:
    shapes: Dict[str, ShapeResult] = {}
    if analysis.domain is None:
        return shapes
    for path, info in analysis.targets.items():
        if info.classification is Classification.READ_AFTER_WRITE:
            shapes[path] = _resolve_target(info, analysis)
    return shapes


def _resolve_target(info: TargetInfo, analysis: BlockAnalysis) -> ShapeResult:
    if analysis.domain is None:
        return ShapeResult(DependencyShape.UNRESOLVED)
    proven = analysis.domain.proven
    if len(proven) != 1:
        return ShapeResult(DependencyShape.UNRESOLVED)
    var = next(iter(proven))
    if not all(isinstance(w, ast.Name) and w.id == var for w in info.write_indexes):
        return ShapeResult(DependencyShape.UNRESOLVED)
    return _combine([_resolve_read(r, var) for r in info.read_indexes])


def _resolve_read(expr: ast.expr, var: str) -> ShapeResult:
    if isinstance(expr, ast.Name) and expr.id == var:
        return ShapeResult(DependencyShape.SELF_CONTAINED)

    if isinstance(expr, ast.BinOp) and isinstance(expr.left, ast.Name) and expr.left.id == var:
        if isinstance(expr.op, ast.Sub):
            k = _const_int(expr.right)
            if k is not None and k > 0:
                return ShapeResult(DependencyShape.MONOTONIC_OFFSET, offset=k)
        if isinstance(expr.op, ast.FloorDiv):
            c = _const_int(expr.right)
            if c is not None and c > 1:
                return ShapeResult(DependencyShape.RECOGNIZED_DAG, divisor=c)
        if isinstance(expr.op, ast.RShift):
            k = _const_int(expr.right)
            if k is not None and 1 <= k < 64:
                return ShapeResult(DependencyShape.RECOGNIZED_DAG, divisor=2**k)

    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Mod):
        inner = expr.left
        if (
            isinstance(inner, ast.BinOp)
            and isinstance(inner.op, (ast.Add, ast.Sub))
            and isinstance(inner.left, ast.Name)
            and inner.left.id == var
        ):
            k = _const_int(inner.right)
            if k is not None and k != 0:
                return ShapeResult(DependencyShape.MODULAR_SELF_REFERENCE)

    return ShapeResult(DependencyShape.UNRESOLVED)


def _combine(results: List[ShapeResult]) -> ShapeResult:
    shapes = {r.shape for r in results}
    if DependencyShape.UNRESOLVED in shapes:
        return ShapeResult(DependencyShape.UNRESOLVED)
    if DependencyShape.MODULAR_SELF_REFERENCE in shapes:
        return ShapeResult(DependencyShape.MODULAR_SELF_REFERENCE)
    if DependencyShape.MONOTONIC_OFFSET in shapes:
        offset = max(r.offset for r in results if r.offset is not None)
        return ShapeResult(DependencyShape.MONOTONIC_OFFSET, offset=offset)
    if DependencyShape.RECOGNIZED_DAG in shapes:
        divisor = min(r.divisor for r in results if r.divisor is not None)
        return ShapeResult(DependencyShape.RECOGNIZED_DAG, divisor=divisor)
    return ShapeResult(DependencyShape.SELF_CONTAINED)


_MAX_MAGNITUDE = 2**63


def _const_int(node: ast.expr) -> Optional[int]:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _const_int(node.operand)
        return -inner if inner is not None else None
    if isinstance(node, ast.BinOp):
        left, right = _const_int(node.left), _const_int(node.right)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                value = left * right
            elif isinstance(node.op, ast.Pow) and 0 <= right < 64:
                value = left**right
            elif isinstance(node.op, ast.LShift) and 0 <= right < 64:
                value = left << right
            elif isinstance(node.op, ast.FloorDiv) and right != 0:
                value = left // right
            else:
                return None
        except (OverflowError, ValueError):
            return None
        return value if abs(value) <= _MAX_MAGNITUDE else None
    return None
