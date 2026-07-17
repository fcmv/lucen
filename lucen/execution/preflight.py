from __future__ import annotations

import builtins
import inspect
import pickle
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from lucen.analysis import purity
from lucen.support import config
from lucen.support.errors import PreflightCheckError


@dataclass
class GateResult:
    refusal: Optional[PreflightCheckError] = None
    reduce_fn: Optional[Callable] = None
    reduce_identity: Any = None
    reduce_tree: bool = True
    on_error_handler: Optional[Callable] = None
    max_errors: Optional[int] = None
    progress_cb: Optional[Callable] = None
    on_fallback_handler: Optional[Callable] = None
    on_timeout_handler: Optional[Callable] = None

    @property
    def ok(self) -> bool:
        return self.refusal is None


def check(spec, env: Dict[str, Any], module_globals: Optional[Dict[str, Any]]) -> GateResult:
    result = GateResult()
    clauses = spec.clauses

    def refuse(message: str) -> GateResult:
        result.refusal = PreflightCheckError(message, file=spec.filename, line=spec.line)
        return result

    reduce_cv = clauses.get("reduce")
    if reduce_cv is not None and reduce_cv.kind == "call":
        kwargs = reduce_cv.value.kwargs
        fn = _resolve(kwargs["fn"].value, env, module_globals)
        if fn is None:
            return refuse(f"reduce=custom fn '{kwargs['fn'].value}' is not resolvable")
        identity = kwargs["identity"].value
        try:
            fn(identity, identity)
        except Exception as exc:
            return refuse(
                f"reduce=custom smoke test fn(identity, identity) "
                f"raised {type(exc).__name__}: {exc}"
            )
        result.reduce_fn = fn
        result.reduce_identity = identity
        tree = kwargs.get("tree")
        result.reduce_tree = tree.value if tree is not None else True

    on_error = clauses.get("on_error")
    if on_error is not None and on_error.kind == "call":
        call = on_error.value
        if call.base.value == "custom":
            handler = _resolve(call.kwargs["handler"].value, env, module_globals)
            if handler is None or not _accepts_two(handler):
                return refuse(
                    "on_error=custom handler must be a callable "
                    "accepting (iteration_index, exception)"
                )
            result.on_error_handler = handler
        else:
            result.max_errors = call.kwargs["max_errors"].value

    progress = clauses.get("progress")
    if progress is not None and progress.kind == "call":
        call = progress.value
        include = call.kwargs.get("include_result")
        if include is not None and include.value is True:
            return refuse("progress include_result=true is not supported by the v1 runtime")
        cb = _resolve(call.args[0].value, env, module_globals)
        if cb is None:
            return refuse(f"progress callback '{call.args[0].value}' is not resolvable")
        result.progress_cb = cb

    on_fallback = clauses.get("on_fallback")
    if (
        on_fallback is not None
        and on_fallback.kind == "call"
        and on_fallback.value.base.value == "custom"
    ):
        handler = _resolve(on_fallback.value.kwargs["handler"].value, env, module_globals)
        if handler is None:
            return refuse("on_fallback=custom handler is not resolvable")
        result.on_fallback_handler = handler

    timeout = clauses.get("timeout")
    if timeout is not None and timeout.kind == "call":
        on_timeout = timeout.value.kwargs.get("on_timeout")
        if on_timeout is not None:
            handler = _resolve(on_timeout.value, env, module_globals)
            if handler is None:
                return refuse("timeout on_timeout handler is not resolvable")
            result.on_timeout_handler = handler

    if not trusts(spec, "callables"):
        trusted = spec.trusted_names | config.active().trust_callables
        for path in getattr(spec, "called_paths", ()):
            if path in trusted or path.split(".", 1)[0] in trusted:
                continue
            fn = _resolve(path, env, module_globals)
            if fn is None:
                continue
            verdict, reason = purity.classify(fn)
            if verdict == purity.IMPURE:
                return refuse(
                    f"helper '{path}' {reason}; its state or effects would "
                    "diverge under parallel execution - mark its def with "
                    "# LUCEN TRUST, add trust=callables to the block, or "
                    "list it under [trust] callables in lucen.toml (spec 7)"
                )

    return result


def trusts(spec, what: str) -> bool:
    cv = spec.clauses.get("trust")
    if cv is None or cv.kind != "name":
        return False
    return cv.value == what or cv.value == "all"


def first_chunk_picklable(args: tuple) -> Optional[str]:
    try:
        pickle.dumps(args, protocol=pickle.HIGHEST_PROTOCOL)
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _resolve(
    name: str, env: Dict[str, Any], module_globals: Optional[Dict[str, Any]]
) -> Optional[Any]:
    root, *rest = name.split(".")
    obj = _MISSING = object()
    for space in (env or {}, module_globals or {}):
        if root in space:
            obj = space[root]
            break
    if obj is _MISSING:
        obj = getattr(builtins, root, _MISSING)
    if obj is _MISSING:
        obj = _import_dotted(name)
        return obj
    try:
        for attr in rest:
            obj = getattr(obj, attr)
    except AttributeError:
        return None
    return obj


def _import_dotted(name: str) -> Optional[Any]:
    import importlib

    parts = name.split(".")
    for split in range(len(parts) - 1, 0, -1):
        module_path = ".".join(parts[:split])
        try:
            obj: Any = importlib.import_module(module_path)
        except ImportError:
            continue
        try:
            for attr in parts[split:]:
                obj = getattr(obj, attr)
        except AttributeError:
            return None
        return obj
    return None


def _accepts_two(handler: Callable) -> bool:
    try:
        inspect.signature(handler).bind(0, ValueError())
        return True
    except TypeError:
        return False
