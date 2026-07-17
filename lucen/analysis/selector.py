from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional

from lucen.analysis.analyzer import DependencyShape, ShapeResult, resolve_shapes
from lucen.analysis.rewriter import AuditTier, BlockAnalysis, Classification
from lucen.analysis.scanner import ClauseValue
from lucen.support.costmodel import (
    estimate_iteration_ns,
    static_iteration_count,
    statically_unprofitable,
)
from lucen.support.errors import (
    DependencyCycleError,
    FallbackRecord,
    LucenError,
    MonotonicDependencyError,
    UnmergeableConflictError,
    UnprofitableParallelismError,
    UnresolvedDependencyShapeError,
    raise_or_fallback,
    report_fallback,
)


class Eligibility(Enum):
    THREAD_CAPABLE = auto()
    WAVEFRONT = auto()
    REDUCTION = auto()
    EARLY_EXIT = auto()
    SEQUENTIAL = auto()


@dataclass
class BlockDecision:
    eligibility: Eligibility
    reasons: List[str] = field(default_factory=list)
    shapes: Dict[str, ShapeResult] = field(default_factory=dict)
    audit_tier: Optional[AuditTier] = None
    dag_divisor: Optional[int] = None
    reduction_ops: Dict[str, str] = field(default_factory=dict)
    unprofitable: bool = False
    asserted_read_residual: bool = False
    fallbacks: List[FallbackRecord] = field(default_factory=list)

    @property
    def routed(self) -> Eligibility:
        if self.unprofitable:
            return Eligibility.SEQUENTIAL
        return self.eligibility


def _free_threaded() -> bool:
    import sys

    probe = getattr(sys, "_is_gil_enabled", None)
    return probe is not None and not probe()


_TIER_RANK = {AuditTier.BY_PROOF: 0, AuditTier.BY_ASSUMPTION: 1, AuditTier.ASSERTED: 2}

_REDUCE_NAME_TO_OP = {
    "sum": "+",
    "prod": "*",
    "min": "min",
    "max": "max",
    "count": "+",
    "any": "|",
    "all": "&",
    "bit_and": "&",
    "bit_or": "|",
    "bit_xor": "^",
    "concat": "+",
}


def select(
    analysis: BlockAnalysis,
    shapes: Optional[Dict[str, ShapeResult]] = None,
    workers: Optional[int] = None,
    experimental: Optional[frozenset] = None,
) -> BlockDecision:
    if shapes is None:
        shapes = resolve_shapes(analysis)
    if experimental is None:
        from lucen.support import config

        experimental = config.active().experimental
    decision = BlockDecision(Eligibility.SEQUENTIAL, shapes=shapes)

    if not analysis.ok:
        decision.reasons.append("analysis fell back; the block runs as unmodified Python")
        decision.fallbacks = list(analysis.fallbacks)
        return decision

    clauses = analysis.block.start.clauses
    filename, lineno = analysis.filename, analysis.block.start.lineno

    backend = clauses.get("backend")
    if backend is not None and backend.kind == "name" and backend.value == "sequential":
        decision.reasons.append("backend=sequential requested")
        return decision

    if analysis.has_return:
        _downgrade_info(
            decision,
            clauses,
            MonotonicDependencyError,
            "early_exit",
            "block contains return; runs SEQUENTIAL (return-value propagation "
            "is outside the experimental early-exit scheduler, spec 5.9.1)",
            "EarlyExitRouting",
            filename,
            lineno,
        )
        return decision
    if analysis.has_break and "early_exit" not in experimental:
        _downgrade_info(
            decision,
            clauses,
            MonotonicDependencyError,
            "early_exit",
            "block contains break; the stable release runs it SEQUENTIAL "
            "(enable activate(experimental=['early_exit']), spec 5.9.1)",
            "EarlyExitRouting",
            filename,
            lineno,
        )
        return decision
    early_exit = analysis.has_break and "early_exit" in experimental

    depend = clauses.get("depend")
    asserted_none = depend is not None and depend.kind == "name" and depend.value == "none"
    asserted_acyclic = (
        depend is not None and depend.kind == "call" and depend.value.base.value == "acyclic"
    )

    needs_thread = False
    dag_divisor: Optional[int] = None
    tier: Optional[AuditTier] = None

    def bump_tier(new: AuditTier) -> None:
        nonlocal tier
        if tier is None or _TIER_RANK[new] > _TIER_RANK[tier]:
            tier = new

    for path, info in sorted(analysis.targets.items()):
        cls = info.classification
        if cls in (Classification.LOOP_LOCAL, Classification.OUTER_READONLY):
            continue
        if cls is Classification.SHARED_INDEXED_SAFE:
            needs_thread = True
            assert info.audit_tier is not None
            bump_tier(info.audit_tier)
            continue
        if cls is Classification.SHARED_SCALAR:
            op = (
                None
                if info.nested_reduction
                else _reduction_op(info.reduce_op, clauses.get("reduce"))
            )
            if op is None:
                _downgrade_error(
                    decision,
                    clauses,
                    UnmergeableConflictError(
                        f"'{path}' is written across iterations with no recognized merge shape",
                        file=filename,
                        line=lineno,
                    ),
                    "unmergeable",
                )
                decision.reasons.append(
                    f"'{path}': shared scalar with no recognized reduction operator"
                )
                return decision
            decision.reduction_ops[path] = op
            continue
        if cls is Classification.SHARED_INDEXED_UNRESOLVED:
            if asserted_none or asserted_acyclic:
                needs_thread = needs_thread or asserted_none
                bump_tier(AuditTier.ASSERTED)
                decision.reasons.append(
                    f"'{path}': safety asserted via depend=; "
                    "write-set audit still runs (spec 5.7.3 tier C)"
                )
                continue
            _downgrade_error(
                decision,
                clauses,
                UnresolvedDependencyShapeError(
                    f"'{path}' is written at an index that is not provably distinct per iteration",
                    file=filename,
                    line=lineno,
                ),
                "unresolved",
            )
            decision.reasons.append(
                f"'{path}': unresolved write index (assert with depend=none if provable)"
            )
            return decision
        shape = shapes[path]
        if shape.shape is DependencyShape.SELF_CONTAINED:
            needs_thread = True
            bump_tier(AuditTier.BY_PROOF)
        elif shape.shape is DependencyShape.RECOGNIZED_DAG:
            assert shape.divisor is not None
            dag_divisor = shape.divisor if dag_divisor is None else min(dag_divisor, shape.divisor)
        elif shape.shape is DependencyShape.MONOTONIC_OFFSET:
            _downgrade_info(
                decision,
                clauses,
                MonotonicDependencyError,
                "monotonic",
                f"'{path}' depends on '{path}[i - {shape.offset}]': a genuine "
                "one-directional chain; no parallel execution exists for this "
                "shape in v1 (spec 5.5.2)",
                "MonotonicDependency",
                filename,
                lineno,
            )
            return decision
        elif shape.shape is DependencyShape.MODULAR_SELF_REFERENCE:
            _downgrade_error(
                decision,
                clauses,
                DependencyCycleError(
                    f"'{path}' has a modular self-reference - a genuine cycle (spec 5.5.4)",
                    file=filename,
                    line=lineno,
                ),
                "modular",
            )
            decision.reasons.append(f"'{path}': modular self-reference cycle")
            return decision
        else:
            if asserted_none or asserted_acyclic:
                needs_thread = needs_thread or asserted_none
                bump_tier(AuditTier.ASSERTED)
                decision.reasons.append(
                    f"'{path}': cross-iteration read asserted disjoint via "
                    "depend=; writes are audited but this READ is NOT verified "
                    "-- ensure no iteration reads an index another writes"
                )
                decision.asserted_read_residual = True
                continue
            _downgrade_error(
                decision,
                clauses,
                UnresolvedDependencyShapeError(
                    f"'{path}' has a cross-iteration read that matches no "
                    "recognized closed form (spec 5.5.5)",
                    file=filename,
                    line=lineno,
                ),
                "unresolved",
            )
            decision.reasons.append(f"'{path}': unresolved dependency shape")
            return decision

    if asserted_acyclic:
        decision.eligibility = Eligibility.WAVEFRONT
        decision.reasons.append(
            "user-asserted acyclic ordering (depend=acyclic); scheduled via the "
            "generalized wavefront (spec 5.8)"
        )
    elif dag_divisor is not None:
        decision.eligibility = Eligibility.WAVEFRONT
        decision.dag_divisor = dag_divisor
        decision.reasons.append(
            f"recognized DAG shape (divisor {dag_divisor}); level-synchronous "
            "wavefront, no task ever blocks (spec 5.8)"
        )
    elif decision.reduction_ops:
        decision.eligibility = Eligibility.REDUCTION
        ops = ", ".join(f"{k} ({v})" for k, v in sorted(decision.reduction_ops.items()))
        decision.reasons.append(f"recognized reduction: {ops}")
    elif needs_thread:
        decision.eligibility = Eligibility.THREAD_CAPABLE
        decision.reasons.append(
            "all shared writes are provably or assertedly disjoint per iteration"
        )
    else:
        decision.eligibility = Eligibility.THREAD_CAPABLE
        decision.reasons.append("no shared state is written; iterations are independent")
    decision.audit_tier = tier

    if early_exit:
        if decision.eligibility is Eligibility.THREAD_CAPABLE:
            decision.eligibility = Eligibility.EARLY_EXIT
            decision.reasons.append(
                "break present; experimental early-exit scheduler runs chunks "
                "speculatively and commits the prefix up to the lowest break "
                "(spec 5.9.1)"
            )
        else:
            _downgrade_info(
                decision,
                clauses,
                MonotonicDependencyError,
                "early_exit",
                "break is only supported over flat indexed/self-contained "
                "blocks in the experimental early-exit scheduler; runs "
                "SEQUENTIAL (spec 5.9.1)",
                "EarlyExitRouting",
                filename,
                lineno,
            )
            decision.eligibility = Eligibility.SEQUENTIAL
            return decision

    _prescreen(decision, analysis, clauses, workers, filename, lineno)
    return decision


def _prescreen(
    decision: BlockDecision,
    analysis: BlockAnalysis,
    clauses: Dict[str, ClauseValue],
    workers: Optional[int],
    filename: str,
    lineno: int,
) -> None:
    calibrate = clauses.get("calibrate")
    if calibrate is not None and calibrate.kind == "literal" and calibrate.value is False:
        return
    for_node = analysis.for_node
    if for_node is None:
        return
    n = static_iteration_count(for_node)
    if n is None:
        return
    per_iter = estimate_iteration_ns(for_node)
    if statically_unprofitable(per_iter, n, workers):
        _downgrade_info(
            decision,
            clauses,
            UnprofitableParallelismError,
            "unprofitable",
            f"parallel-eligible, but ~{per_iter} ns/iteration x {n} iterations is "
            "predicted to lose to dispatch overhead by a wide margin (estimate; "
            "override with calibrate=false, spec 5.17)",
            "PARALLEL_UNPROFITABLE",
            filename,
            lineno,
        )
        decision.unprofitable = True


def _reduction_op(inferred: Optional[str], reduce_clause: Optional[ClauseValue]) -> Optional[str]:
    if reduce_clause is not None:
        if reduce_clause.kind == "name":
            return _REDUCE_NAME_TO_OP.get(reduce_clause.value)
        if reduce_clause.kind == "call":
            return "custom"
    return inferred


def _strict_requests_hard(strict: Optional[ClauseValue], reason_key: str) -> bool:
    if strict is None:
        return False
    if strict.kind == "literal":
        return strict.value is True
    if strict.kind == "call" and strict.value.base.value is True:
        allow = strict.value.kwargs.get("allow")
        allowed = {item.value for item in allow.value} if allow is not None else set()
        return reason_key not in allowed
    return False


def _downgrade_error(
    decision: BlockDecision, clauses: Dict[str, ClauseValue], exc: LucenError, reason_key: str
) -> None:
    if _strict_requests_hard(clauses.get("strict"), reason_key):
        raise exc
    decision.fallbacks.append(raise_or_fallback(exc))


def _downgrade_info(
    decision: BlockDecision,
    clauses: Dict[str, ClauseValue],
    exc_type: type,
    reason_key: str,
    message: str,
    label: str,
    filename: str,
    lineno: int,
) -> None:
    if _strict_requests_hard(clauses.get("strict"), reason_key):
        raise exc_type(message, file=filename, line=lineno)
    decision.fallbacks.append(report_fallback(message, file=filename, line=lineno, error=label))
    decision.reasons.append(message)
