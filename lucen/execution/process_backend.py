from __future__ import annotations

import array
import ast
import atexit
import importlib
import multiprocessing
import os
import pickle
import sys
import threading
import time
import types
from concurrent.futures import FIRST_EXCEPTION, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Dict, List, Optional, Tuple

from lucen.execution.runtime import SKIP
from lucen.support.errors import (
    MidRunSerializationError,
    ParallelTimeoutError,
    PreflightCheckError,
    report_fallback,
)

_pool: Optional[ProcessPoolExecutor] = None
_pool_lock = threading.Lock()


def _ensure_pool(workers: int) -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            size = min(workers, os.cpu_count() or 4)
            _pool = ProcessPoolExecutor(max_workers=size)
            atexit.register(shutdown)
        return _pool


def shutdown() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=True)
            _pool = None


def _recycle() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
            _pool = None


_spawn_safety: Dict[str, Tuple[bool, str]] = {}


# spawn re-imports the entry module in every worker; an unguarded script
# would re-run its top-level work there
def _spawn_entry_safe() -> Tuple[bool, str]:
    method = multiprocessing.get_start_method(allow_none=True)
    if method is None:
        method = "spawn" if sys.platform in ("win32", "darwin") else "fork"
    if method != "spawn":
        return True, ""
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    if path is None:
        return True, ""
    cached = _spawn_safety.get(path)
    if cached is not None:
        return cached
    try:
        with open(path, "rb") as f:
            tree = ast.parse(f.read())
    except Exception:  # noqa: BLE001 - unparsable -> don't block
        result = (True, "")
    else:
        result = (True, "")
        if not any(_is_main_guard(node) for node in tree.body):
            for node in tree.body:
                if not _import_time_safe(node):
                    result = (False, os.path.basename(path))
                    break
    _spawn_safety[path] = result
    return result


def _is_main_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    parts = [test.left] + list(test.comparators)
    return (
        len(parts) == 2
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and any(isinstance(p, ast.Name) and p.id == "__name__" for p in parts)
        and any(isinstance(p, ast.Constant) and p.value == "__main__" for p in parts)
    )


def _import_time_safe(node: ast.stmt) -> bool:
    if isinstance(
        node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    ):
        return True
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        return True
    if isinstance(node, ast.Assign):
        return _literal_only(node.value)
    if isinstance(node, ast.AnnAssign):
        return node.value is None or _literal_only(node.value)
    return False


def _literal_only(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp):
        return _literal_only(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_literal_only(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            k is not None and _literal_only(k) and _literal_only(v)
            for k, v in zip(node.keys, node.values)
        )
    return False


# modules do not pickle; ship the name and re-import in the child
class _ModuleRef:
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __reduce__(self):
        return (importlib.import_module, (self.name,))


def _portable(value: Any) -> Any:
    if isinstance(value, types.ModuleType):
        return _ModuleRef(value.__name__)
    return value


class _OffsetSeq:
    __slots__ = ("_data", "_offset")

    def __init__(self, data: list, offset: int):
        self._data = data
        self._offset = offset

    def __getitem__(self, pos: int):
        return self._data[pos - self._offset]


def _supports_slice(value: Any) -> bool:
    try:
        return value[0:0] is not value
    except Exception:  # noqa: BLE001 - any failure -> ship whole
        return False


_fn_cache: Dict[int, Any] = {}


def _compiled(source: str, name: str):
    key = hash(source)
    fn = _fn_cache.get(key)
    if fn is None:
        namespace: Dict[str, Any] = {}
        exec(compile(source, f"<lucen:{name}>", "exec"), {"__builtins__": {}}, namespace)
        fn = namespace[name]
        _fn_cache[key] = fn
    return fn


def _make_slab(kind, size: int):
    if kind == "list":
        return [SKIP] * size
    if kind == "dict":
        return {}
    if kind[0] == "array":
        return array.array(kind[1], bytes(array.array(kind[1]).itemsize * size))
    if kind[0] == "bytes":
        return bytearray(size)
    return [SKIP] * size


class _PayloadUnpicklable(Exception):
    pass


def _run_chunk_task(source: str, name: str, params: Tuple[str, ...], payload: bytes):
    from lucen.execution import nested_guard

    try:
        values, meta = pickle.loads(payload)
    except Exception as exc:  # noqa: BLE001 - parent maps to fallback
        raise _PayloadUnpicklable(f"{type(exc).__name__}: {exc}") from None
    fn = _compiled(source, name)
    a, b = meta["bounds"]
    slabs = {p: _make_slab(kind, b - a) for p, kind in meta["slabs"]}
    sites = {p: [SKIP] * (b - a) for p in meta["sites"]}
    errors: List[Tuple[int, BaseException]] = []
    args = []
    for p in params:
        if p == "_plx_indices":
            args.append(meta["indices"])
        elif p == "_plx_seq":
            args.append(values["_plx_seq"])
        elif p in slabs:
            args.append(slabs[p])
        elif p in sites:
            args.append(sites[p])
        elif p == "_plx_errors":
            args.append(errors)
        elif p == "_plx_clock":
            args.append(time.monotonic)
        elif p == "_plx_deadline":
            args.append(meta["deadline"] if meta["deadline"] is not None else float("inf"))
        elif p == "_plx_timeout_error":
            args.append(meta["timeout_error"])
        elif p == "_plx_progress":
            args.append(lambda _i: None)
        else:
            args.append(values[p])
    error: Optional[BaseException] = None
    with nested_guard.dispatch_scope():
        try:
            fn(*args)
        except BaseException as exc:  # noqa: BLE001 - rethrown by the parent
            error = _picklable(exc)
    return slabs, sites, [(i, _picklable(e)) for i, e in errors], error


class _ExceptionProxy(Exception):
    def __init__(self, module: str, qualname: str, message: str):
        super().__init__(module, qualname, message)
        self.module = module
        self.qualname = qualname
        self.message = message


def _picklable(exc: BaseException) -> BaseException:
    try:
        pickle.loads(pickle.dumps(exc))
        return exc
    except Exception:
        return _ExceptionProxy(type(exc).__module__, type(exc).__qualname__, str(exc))


def _rehydrate(exc: Optional[BaseException]) -> Optional[BaseException]:
    if not isinstance(exc, _ExceptionProxy):
        return exc
    try:
        obj: Any = importlib.import_module(exc.module)
        for part in exc.qualname.split("."):
            obj = getattr(obj, part)
        if isinstance(obj, type) and issubclass(obj, BaseException):
            return obj(exc.message)
    except Exception:  # noqa: BLE001 - any failure -> annotated fallback
        pass
    return RuntimeError(f"{exc.qualname}: {exc.message} (original exception was not picklable)")


def run_chunks(spec, plan, bounds, env, module_globals, gate, workers, deadline, stats):
    from lucen.execution import dispatch as _dispatch
    from lucen.execution.dispatch import _value_of
    from lucen.execution.runtime import resolve_path
    from lucen.support import config

    safe, entry = _spawn_entry_safe()
    if not safe:
        raise PreflightCheckError(
            f"entry script {entry} runs work at import time without an "
            'if __name__ == "__main__" guard; PROCESS workers on a spawn '
            "platform would re-execute it in every worker - add the guard",
            file=spec.filename,
            line=spec.line,
        )

    pool = _ensure_pool(workers)
    stats["backend"] = "process"
    stats["workers"] = max(stats["workers"], min(workers, len(bounds)))
    artifact = spec.artifact

    special = {
        "_plx_indices",
        "_plx_seq",
        "_plx_errors",
        "_plx_clock",
        "_plx_deadline",
        "_plx_timeout_error",
        "_plx_progress",
    }
    slab_names = {p.param for p in artifact.slabs}
    site_names = {p for r in artifact.reductions for p in r.site_params}
    user_names = [p for p in artifact.params if p not in special | slab_names | site_names]

    all_values = {p: _portable(_value_of(p, env, module_globals)) for p in user_names}
    # only params read solely as P[i] ship as per-chunk slices; anything that
    # does not slice positionally ships whole so results cannot change
    sliced_names = [
        p for p in artifact.sliceable if p in all_values and _supports_slice(all_values[p])
    ]
    sliced_full = {p: all_values[p] for p in sliced_names}
    base_values = {p: v for p, v in all_values.items() if p not in sliced_full}

    typed_buffers = "typed_buffers" in config.active().experimental

    def _slab_kind(slab_plan):
        if not (artifact.dense and typed_buffers):
            return slab_plan.kind
        try:
            container = resolve_path(env, slab_plan.container)
        except (KeyError, AttributeError, TypeError):
            return slab_plan.kind
        if isinstance(container, array.array):
            return ("array", container.typecode)
        if isinstance(container, bytearray):
            return ("bytes",)
        return slab_plan.kind

    meta_base = {
        "slabs": [(p.param, _slab_kind(p)) for p in artifact.slabs],
        "sites": sorted(site_names),
    }
    if artifact.per_task_deadline:
        meta_base["deadline"] = deadline
        meta_base["timeout_error"] = ParallelTimeoutError(
            "per-iteration timeout= deadline exceeded", file=spec.filename, line=spec.line
        )

    def payload_for(a: int, b: int):
        values = dict(base_values)
        indices = plan.indices(a, b)
        if artifact.domain != "range":
            values["_plx_seq"] = _OffsetSeq(plan.seq[a:b], a)
        elif sliced_full:
            lo, hi = indices[0], indices[-1]
            if lo > hi:
                lo, hi = hi, lo
            for p, full in sliced_full.items():
                values[p] = _OffsetSeq(full[lo : hi + 1], lo)
        meta = dict(meta_base)
        meta["bounds"] = (a, b)
        meta["indices"] = indices
        return values, meta

    first_values, first_meta = payload_for(*bounds[0])
    problem, blob = _pickle_problem(
        (artifact.source, artifact.name, artifact.params, first_values, first_meta)
    )
    if problem is not None:
        raise PreflightCheckError(
            f"first chunk's argument bundle is not picklable ({problem}); "
            "PROCESS dispatch is impossible",
            file=spec.filename,
            line=spec.line,
        )
    if not _trusts(spec, "pickle") and not _pickle_stable(blob):
        raise PreflightCheckError(
            "first chunk's argument bundle does not survive a pickle round-trip "
            "byte-identically (a __reduce__/__getstate__ returning different "
            "data?); values would change across the process boundary - "
            "trust=pickle asserts they are safe",
            file=spec.filename,
            line=spec.line,
        )

    futures = {}
    params = tuple(artifact.params)
    for idx, (a, b) in enumerate(bounds, start=1):
        values, meta = (first_values, first_meta) if idx == 1 else payload_for(a, b)
        problem, payload = _pickle_problem((values, meta))
        if problem is not None:
            for fut in futures:
                fut.cancel()
            wait([f for f in futures if not f.cancelled()])
            raise MidRunSerializationError(
                f"chunk {idx} argument bundle stopped being picklable "
                f"({problem}); re-running sequentially",
                file=spec.filename,
                line=spec.line,
            )
        # the bundle is pickled once, above; bytes re-pickle as a memcpy
        fut = pool.submit(_run_chunk_task, artifact.source, artifact.name, params, payload)
        futures[fut] = (idx, a, b)

    timeout_s = max(0.0, deadline - time.monotonic()) if deadline is not None else None
    done, not_done = wait(futures, timeout=timeout_s, return_when=FIRST_EXCEPTION)

    if not_done and deadline is not None and time.monotonic() >= deadline:
        for fut in not_done:
            fut.cancel()
        _recycle()
        report_fallback(
            "timeout= hard-kill recycled the process pool (spec 7)",
            file=spec.filename,
            line=spec.line,
            error="PoolRecycle",
        )
        records = _records_of(done, futures, spec, plan)
        _dispatch._commit_records(
            spec,
            plan,
            _dispatch._contiguous_prefix(sorted(records, key=lambda r: r.idx)),
            env,
            module_globals,
            gate,
        )
        exc = ParallelTimeoutError(
            "block exceeded its timeout= bound", file=spec.filename, line=spec.line
        )
        if gate.on_timeout_handler is not None:
            gate.on_timeout_handler(exc)
        raise exc
    if not_done:
        finished, _ = wait(not_done)
        done |= finished

    records = _records_of(done, futures, spec, plan)
    errored = sorted((r for r in records if r.error is not None), key=lambda r: r.idx)
    first_error = (errored[0].idx, errored[0].error) if errored else None
    return records, first_error


def _records_of(done, futures, spec, plan):
    from lucen.execution.dispatch import _Record

    records = []
    for fut in done:
        if fut.cancelled():
            continue
        idx, a, b = futures[fut]
        try:
            slabs, sites, errors, error = fut.result()
        # old workers lack modules that joined sys.path after the pool
        # spawned; recycle so the next dispatch sees the current path
        except _PayloadUnpicklable as exc:
            _recycle()
            raise MidRunSerializationError(
                f"chunk argument bundle could not be reconstructed in the "
                f"worker ({exc}); re-running sequentially",
                file=spec.filename,
                line=spec.line,
            ) from exc
        except BrokenProcessPool as exc:
            _recycle()
            raise MidRunSerializationError(
                f"process pool broke mid-dispatch ({exc}); re-running sequentially",
                file=spec.filename,
                line=spec.line,
            ) from exc
        record = _Record(
            idx,
            a,
            b,
            slabs,
            sites,
            errors=[(i, _rehydrate(e)) for i, e in errors],
            error=_rehydrate(error),
        )
        records.append(record)
    return records


def _pickle_problem(payload) -> Tuple[Optional[str], Optional[bytes]]:
    try:
        return None, pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}", None


def _trusts(spec, what: str) -> bool:
    from lucen.execution.preflight import trusts

    return trusts(spec, what)


def _pickle_stable(blob: bytes) -> bool:
    # compare gen1 to gen2, never gen0: the parent dump embeds string-interning
    # identity a load cannot reproduce, so honest bundles converge after one
    # round trip while an accumulating __reduce__ never does
    try:
        gen1 = pickle.dumps(pickle.loads(blob), protocol=pickle.HIGHEST_PROTOCOL)
        gen2 = pickle.dumps(pickle.loads(gen1), protocol=pickle.HIGHEST_PROTOCOL)
        return gen1 == gen2
    except Exception:  # noqa: BLE001
        return True
