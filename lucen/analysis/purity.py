from __future__ import annotations

import ast
import builtins
import inspect
import textwrap
import types
from typing import Any, Dict, Optional, Set, Tuple

from lucen.analysis.rewriter import _MUTATING_METHODS

IMPURE_BUILTIN_NAMES = frozenset(
    {
        "print",
        "open",
        "input",
        "exec",
        "eval",
        "setattr",
        "delattr",
        "__import__",
        "breakpoint",
    }
)
IMPURE_MODULES = frozenset({"random", "logging"})

_MAX_DEPTH = 3
_verdicts: Dict[int, Tuple[str, str]] = {}

PURE = "pure"
IMPURE = "impure"


# only positive proof of impurity ever downgrades a block; anything
# unprovable keeps the args-as-reads trust, so routing never regresses
def classify(
    obj: Any, _depth: int = _MAX_DEPTH, _seen: Optional[Set[int]] = None
) -> Tuple[str, str]:
    if obj is None:
        return PURE, ""
    if isinstance(obj, types.BuiltinFunctionType):
        if getattr(obj, "__module__", None) == "builtins" and obj.__name__ in IMPURE_BUILTIN_NAMES:
            return IMPURE, f"'{obj.__name__}' performs I/O or mutates state"
        return PURE, ""
    if getattr(obj, "__module__", None) in IMPURE_MODULES:
        return IMPURE, (
            f"'{getattr(obj, '__qualname__', obj)}' belongs to the "
            f"stateful module '{obj.__module__}'"
        )
    code = getattr(obj, "__code__", None)
    if code is None:
        return PURE, ""
    cached = _verdicts.get(id(code))
    if cached is not None:
        return cached
    if _seen is None:
        _seen = set()
    if id(code) in _seen or _depth <= 0:
        return PURE, ""
    _seen.add(id(code))
    verdict = _analyze(obj, code, _depth, _seen)
    _verdicts[id(code)] = verdict
    return verdict


def _analyze(fn, code, depth: int, seen: Set[int]) -> Tuple[str, str]:
    try:
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError, IndentationError):
        return PURE, ""
    local_names = set(code.co_varnames) | set(code.co_cellvars)
    fn_globals = getattr(fn, "__globals__", {})

    for node in ast.walk(tree):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            return IMPURE, (
                "declares "
                f"{'global' if isinstance(node, ast.Global) else 'nonlocal'}"
                f" {', '.join(node.names)}"
            )
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AugAssign)
                else node.targets
            )
            for target in targets:
                root = _chain_root(target)
                if root is not None and root not in local_names:
                    return IMPURE, f"writes shared state rooted at '{root}'"
        if isinstance(node, ast.Call):
            impure = _impure_call(node, local_names, fn_globals, depth, seen)
            if impure:
                return IMPURE, impure
    return PURE, ""


def _chain_root(node: ast.expr) -> Optional[str]:
    if not isinstance(node, (ast.Attribute, ast.Subscript)):
        return None
    cur: ast.expr = node
    while isinstance(cur, (ast.Attribute, ast.Subscript)):
        cur = cur.value
    return cur.id if isinstance(cur, ast.Name) else None


def _impure_call(
    node: ast.Call, local_names: Set[str], fn_globals: Dict, depth: int, seen: Set[int]
) -> Optional[str]:
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in _MUTATING_METHODS:
            root = func.value.id if isinstance(func.value, ast.Name) else _chain_root(func.value)
            if root is not None and root not in local_names:
                return f"mutates '{root}' via .{func.attr}()"
        if isinstance(func.value, ast.Name) and func.value.id not in local_names:
            base = fn_globals.get(func.value.id)
            target = getattr(base, func.attr, None) if base is not None else None
            if target is not None:
                verdict, reason = classify(target, depth - 1, seen)
                if verdict == IMPURE:
                    return f"calls '{func.value.id}.{func.attr}' which {reason}"
        return None
    if isinstance(func, ast.Name) and func.id not in local_names:
        target = fn_globals.get(func.id, getattr(builtins, func.id, None))
        if target is not None:
            verdict, reason = classify(target, depth - 1, seen)
            if verdict == IMPURE:
                return f"calls '{func.id}' which {reason}"
    return None


def reset_memo() -> None:
    _verdicts.clear()
