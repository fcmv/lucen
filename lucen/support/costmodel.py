from __future__ import annotations

import ast
import os
from typing import Optional

from lucen.analysis.analyzer import _const_int

_NODE_WEIGHTS_NS = {
    ast.Call: 500,
    ast.Subscript: 40,
    ast.Attribute: 20,
    ast.BinOp: 25,
    ast.Compare: 25,
    ast.BoolOp: 10,
    ast.UnaryOp: 10,
    ast.Name: 5,
    ast.Constant: 2,
}

THREAD_DISPATCH_NS = 20_000
COMMIT_PER_ELEMENT_NS = 10
CHUNKS_PER_WORKER = 4
PROCESS_CHUNKS_PER_WORKER = 2
PRESCREEN_MARGIN = 10

PROCESS_DISPATCH_NS = 200_000
PROCESS_PER_ELEMENT_NS = 200

FT_THREAD_MIN_NS = 10_000


def overhead_ns(chunks: int, elements: int, thread: bool) -> float:
    if thread:
        return chunks * THREAD_DISPATCH_NS + elements * COMMIT_PER_ELEMENT_NS
    return chunks * PROCESS_DISPATCH_NS + elements * PROCESS_PER_ELEMENT_NS


def estimate_iteration_ns(for_node: ast.For) -> int:
    total = 0
    for stmt in for_node.body:
        for node in ast.walk(stmt):
            total += _NODE_WEIGHTS_NS.get(type(node), 0)
    return max(total, 1)


def static_iteration_count(for_node: ast.For) -> Optional[int]:
    it = for_node.iter
    if not (
        isinstance(it, ast.Call)
        and isinstance(it.func, ast.Name)
        and it.func.id == "range"
        and not it.keywords
        and 1 <= len(it.args) <= 3
    ):
        return None
    args = [_const_int(a) for a in it.args]
    if any(a is None for a in args):
        return None
    try:
        return len(range(*[a for a in args if a is not None]))
    except (TypeError, ValueError):
        return None


def statically_unprofitable(
    iteration_ns: int, iterations: int, workers: Optional[int] = None
) -> bool:
    workers = workers or os.cpu_count() or 4
    chunks = min(PROCESS_CHUNKS_PER_WORKER * workers, max(iterations, 1))
    gain = iteration_ns * iterations * (1.0 - 1.0 / max(workers, 2))
    overhead = overhead_ns(chunks, iterations, thread=False)
    return gain * PRESCREEN_MARGIN < overhead
