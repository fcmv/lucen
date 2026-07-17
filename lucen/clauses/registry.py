from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any, Callable, Dict, FrozenSet, Iterable, Optional, Tuple

from lucen.support.errors import ClauseValueError


class Invalid(Exception):
    pass


DOWNGRADE_REASONS: FrozenSet[str] = frozenset(
    {
        "monotonic",
        "unresolved",
        "modular",
        "nested",
        "unprofitable",
        "early_exit",
        "branch_merge",
    }
)

REDUCE_OPS: FrozenSet[str] = frozenset(
    {
        "sum",
        "prod",
        "min",
        "max",
        "count",
        "any",
        "all",
        "bit_and",
        "bit_or",
        "bit_xor",
        "concat",
    }
)

_REMOVED_KEYS: Dict[str, str] = {
    "process_wait": "removed (spec §5.8): recognized-DAG blocks run on "
    "PROCESS via the wavefront driver automatically; delete this clause",
    "batch_size": "renamed to 'chunks=' (spec §7), as a sub-argument of "
    "backend= (e.g. backend=process(chunks=8))",
}


def _check_bool(cv) -> None:
    if cv.kind == "literal" and type(cv.value) is bool:
        return
    raise Invalid(f"expected true or false, got {cv.raw!r}")


def _check_int(cv, minimum: Optional[int] = None) -> None:
    if cv.kind == "literal" and type(cv.value) is int:
        if minimum is not None and cv.value < minimum:
            raise Invalid(f"must be an integer >= {minimum}, got {cv.value}")
        return
    raise Invalid(f"expected an integer, got {cv.raw!r}")


def _check_number(cv, positive: bool = True) -> None:
    if cv.kind == "literal" and type(cv.value) in (int, float):
        if positive and cv.value <= 0:
            raise Invalid(f"must be a number > 0, got {cv.value}")
        return
    raise Invalid(f"expected a number, got {cv.raw!r}")


def _check_name_in(cv, options: Iterable[str]) -> None:
    opts = set(options)
    if cv.kind == "name" and cv.value in opts:
        return
    shown = cv.value if cv.kind == "name" else cv.raw
    hint = get_close_matches(str(shown), sorted(opts), n=1)
    suffix = f" - did you mean '{hint[0]}'?" if hint else ""
    raise Invalid(f"unknown value {shown!r}{suffix} (expected one of: {', '.join(sorted(opts))})")


def _check_callable_ref(cv) -> None:
    if cv.kind == "name":
        return
    raise Invalid(f"expected a callable name (e.g. my_module.my_fn), got {cv.raw!r}")


def _check_list(cv, item_check: Callable[[Any], None], what: str) -> None:
    if cv.kind != "list":
        raise Invalid(f"expected a [...] list of {what}, got {cv.raw!r}")
    for item in cv.value:
        item_check(item)


def _check_int_list(cv) -> None:
    _check_list(cv, lambda i: _check_int(i, 0), "non-negative integers")


def _check_name_list(cv, vocab: Optional[FrozenSet[str]] = None) -> None:
    def item(i) -> None:
        if i.kind != "name":
            raise Invalid(f"expected a name, got {i.raw!r}")
        if vocab is not None and i.value not in vocab:
            _check_name_in(i, vocab)

    _check_list(cv, item, "names")


def _check_call(
    cv,
    *,
    kwargs: Dict[str, Callable],
    required: Tuple[str, ...] = (),
    positional: Tuple[Callable, ...] = (),
) -> None:
    call = cv.value
    if len(call.args) != len(positional):
        raise Invalid(f"takes {len(positional)} positional sub-argument(s), got {len(call.args)}")
    for chk, arg in zip(positional, call.args):
        chk(arg)
    for k, v in call.kwargs.items():
        if k not in kwargs:
            hint = get_close_matches(k, sorted(kwargs), n=1)
            suffix = f" - did you mean '{hint[0]}'?" if hint else ""
            raise Invalid(f"unknown sub-argument '{k}'{suffix}")
        try:
            kwargs[k](v)
        except Invalid as e:
            raise Invalid(f"sub-argument '{k}': {e}") from None
    for r in required:
        if r not in call.kwargs:
            raise Invalid(f"missing required sub-argument '{r}='")


def _any(cv) -> None:
    if cv.kind == "call":
        raise Invalid(f"expected a plain value, got {cv.raw!r}")


def _c_backend(cv) -> None:
    opts = {"thread", "process", "sequential"}
    if cv.kind == "name":
        _check_name_in(cv, opts)
        return
    if cv.kind == "call":
        base = cv.value.base
        _check_name_in(base, opts)
        if base.value == "thread":
            _check_call(
                cv,
                kwargs={
                    "pool_size": lambda v: _check_int(v, 1),
                    "chunks": lambda v: _check_int(v, 0),
                },
            )
        elif base.value == "process":
            _check_call(
                cv,
                kwargs={
                    "chunks": lambda v: _check_int(v, 0),
                    "pool": _check_callable_ref,
                },
            )
        else:
            raise Invalid("backend=sequential takes no sub-arguments")
        return
    raise Invalid(f"expected thread, process, or sequential, got {cv.raw!r}")


def _c_calibrate(cv) -> None:
    if cv.kind == "literal" and type(cv.value) is bool:
        return
    if cv.kind == "name":
        _check_name_in(cv, {"static", "always"})
        return
    if cv.kind == "call":
        base = cv.value.base
        if base.kind == "name" and base.value == "threshold":
            _check_call(
                cv,
                kwargs={"min_gain": lambda v: _check_number(v, positive=True)},
                required=("min_gain",),
            )
            return
        raise Invalid(f"unknown form {cv.raw!r}")
    raise Invalid(
        f"expected true/false, static, always, or threshold(min_gain=...), got {cv.raw!r}"
    )


def _c_nested(cv) -> None:
    _check_name_in(cv, {"sequential", "shared_pool", "independent"})


def _c_depend(cv) -> None:
    if cv.kind == "name":
        _check_name_in(cv, {"none"})
        return
    if cv.kind == "call":
        base = cv.value.base
        if base.kind == "name" and base.value == "acyclic":
            _check_call(cv, kwargs={"order": _check_callable_ref}, required=("order",))
            return
        raise Invalid(f"unknown form {cv.raw!r}")
    raise Invalid(f"expected none or acyclic(order=<callable>), got {cv.raw!r}")


def _c_skip_runtime_check(cv) -> None:
    _check_bool(cv)


def _c_trust(cv) -> None:
    _check_name_in(cv, {"callables", "pickle", "all"})


def _c_on_error(cv) -> None:
    if cv.kind == "name":
        _check_name_in(cv, {"collect"})
        return
    if cv.kind == "call":
        base = cv.value.base
        _check_name_in(base, {"collect", "custom"})
        if base.value == "collect":
            _check_call(cv, kwargs={"max_errors": lambda v: _check_int(v, 1)})
        else:
            _check_call(cv, kwargs={"handler": _check_callable_ref}, required=("handler",))
        return
    raise Invalid(
        f"expected collect, collect(max_errors=N), or custom(handler=...), got {cv.raw!r}"
    )


def _c_strict(cv) -> None:
    if cv.kind == "literal" and type(cv.value) is bool:
        return
    if cv.kind == "call":
        base = cv.value.base
        if base.kind == "literal" and base.value is True:
            _check_call(
                cv,
                kwargs={"allow": lambda v: _check_name_list(v, DOWNGRADE_REASONS)},
                required=("allow",),
            )
            return
        if base.kind == "literal" and base.value is False:
            raise Invalid("strict=false takes no sub-arguments")
        raise Invalid(f"unknown form {cv.raw!r}")
    raise Invalid(f"expected true, false, or true(allow=[...]), got {cv.raw!r}")


def _c_on_fallback(cv) -> None:
    modes = {"hard", "quiet", "report"}
    if cv.kind == "name":
        _check_name_in(cv, modes)
        return
    if cv.kind == "call":
        base = cv.value.base
        _check_name_in(base, modes | {"custom"})
        if base.value == "custom":
            _check_call(cv, kwargs={"handler": _check_callable_ref}, required=("handler",))
        else:
            _check_call(
                cv,
                kwargs={"allow": lambda v: _check_name_list(v, DOWNGRADE_REASONS)},
                required=("allow",),
            )
        return
    raise Invalid(
        f"expected hard/quiet/report, <mode>(allow=[...]), or custom(handler=...), got {cv.raw!r}"
    )


def _c_timeout(cv) -> None:
    if cv.kind == "literal":
        _check_number(cv, positive=True)
        return
    if cv.kind == "call":
        base = cv.value.base
        _check_number(base, positive=True)
        _check_call(
            cv,
            kwargs={
                "per_task": _check_bool,
                "on_timeout": _check_callable_ref,
            },
        )
        return
    raise Invalid(f"expected a positive number of seconds, got {cv.raw!r}")


def _c_reduction_order(cv) -> None:
    if cv.kind == "name":
        _check_name_in(cv, {"sequential_equivalent", "stable"})
        return
    if cv.kind == "call":
        base = cv.value.base
        if base.kind == "name" and base.value == "custom":
            _check_call(cv, kwargs={"combine": _check_callable_ref}, required=("combine",))
            return
        raise Invalid(f"unknown form {cv.raw!r}")
    raise Invalid(f"expected sequential_equivalent, stable, or custom(combine=...), got {cv.raw!r}")


def _c_reduce(cv) -> None:
    if cv.kind == "name":
        _check_name_in(cv, REDUCE_OPS)
        return
    if cv.kind == "call":
        base = cv.value.base
        if base.kind == "name" and base.value == "custom":
            _check_call(
                cv,
                kwargs={
                    "fn": _check_callable_ref,
                    "identity": _any,
                    "tree": _check_bool,
                },
                required=("fn", "identity"),
            )
            return
        raise Invalid(f"unknown form {cv.raw!r}")
    raise Invalid(f"expected a built-in op name or custom(fn=..., identity=...), got {cv.raw!r}")


def _c_grainsize(cv) -> None:
    if cv.kind == "literal":
        _check_int(cv, 1)
        return
    if cv.kind == "call":
        base = cv.value.base
        _check_int(base, 1)
        _check_call(cv, kwargs={"min_workers": lambda v: _check_int(v, 1)})
        return
    raise Invalid(f"expected an integer level width, got {cv.raw!r}")


def _c_progress(cv) -> None:
    if cv.kind == "literal" and type(cv.value) is bool:
        return
    if cv.kind == "call":
        base = cv.value.base
        if base.kind == "name" and base.value == "callback":
            _check_call(
                cv,
                kwargs={
                    "per_task": _check_bool,
                    "include_result": _check_bool,
                },
                positional=(_check_callable_ref,),
            )
            return
        raise Invalid(f"unknown form {cv.raw!r}")
    raise Invalid(f"expected true, false, or callback(<callable>), got {cv.raw!r}")


def _c_affinity(cv) -> None:
    if cv.kind == "name":
        _check_name_in(cv, {"compact", "scatter"})
        return
    if cv.kind == "call":
        base = cv.value.base
        if base.kind == "name" and base.value == "explicit":
            _check_call(
                cv,
                kwargs={
                    "cores": _check_int_list,
                    "numa_node": lambda v: _check_int(v, 0),
                },
                required=("cores",),
            )
            return
        raise Invalid(f"unknown form {cv.raw!r}")
    raise Invalid(f"expected compact, scatter, or explicit(cores=[...]), got {cv.raw!r}")


def _c_args(cv) -> None:
    if cv.kind == "name":
        _check_name_in(cv, {"checked", "unchecked"})
        return
    if cv.kind == "call":
        base = cv.value.base
        if base.kind == "name" and base.value == "unchecked":
            _check_call(
                cv,
                kwargs={
                    "only": lambda v: _check_name_list(v),
                    "skip_runtime_check": _check_bool,
                },
            )
            return
        raise Invalid(f"unknown form {cv.raw!r}")
    raise Invalid(f"expected checked, unchecked, or unchecked(only=[...]), got {cv.raw!r}")


def _c_qualname(cv) -> None:
    if cv.kind == "name":
        return
    if cv.kind == "call":
        base = cv.value.base
        if base.kind != "name":
            raise Invalid(f"expected a qualified name, got {base.raw!r}")
        _check_call(cv, kwargs={"module": _check_callable_ref})
        return
    raise Invalid(f"expected a qualified name, got {cv.raw!r}")


@dataclass(frozen=True)
class ClauseSpec:
    key: str
    hosts: FrozenSet[str]
    check: Callable[[Any], None]
    accepts: str


def _spec(key: str, hosts: str, check: Callable, accepts: str) -> ClauseSpec:
    return ClauseSpec(key, frozenset(hosts.split()), check, accepts)


REGISTRY: Dict[str, ClauseSpec] = {
    s.key: s
    for s in [
        _spec(
            "backend",
            "START",
            _c_backend,
            "thread | process | sequential | thread(pool_size=N, chunks=M) | "
            "process(chunks=M, pool=<factory>)",
        ),
        _spec(
            "calibrate",
            "START",
            _c_calibrate,
            "true | false | static | always | threshold(min_gain=<float>)",
        ),
        _spec("nested", "START", _c_nested, "sequential | shared_pool | independent"),
        _spec("depend", "START", _c_depend, "none | acyclic(order=<callable>)"),
        _spec("skip_runtime_check", "START", _c_skip_runtime_check, "true | false"),
        _spec("trust", "START", _c_trust, "callables | pickle | all"),
        _spec(
            "on_error",
            "START",
            _c_on_error,
            "collect | collect(max_errors=N) | custom(handler=<callable>)",
        ),
        _spec("strict", "START", _c_strict, "true | false | true(allow=[reason, ...])"),
        _spec(
            "on_fallback",
            "START",
            _c_on_fallback,
            "hard | quiet | report | <mode>(allow=[reason, ...]) | custom(handler=<callable>)",
        ),
        _spec(
            "timeout",
            "START",
            _c_timeout,
            "<seconds> | <seconds>(per_task=true[, on_timeout=<callable>])",
        ),
        _spec(
            "reduction_order",
            "START",
            _c_reduction_order,
            "sequential_equivalent | stable | custom(combine=<callable>)",
        ),
        _spec(
            "reduce",
            "START",
            _c_reduce,
            f"one of {{{', '.join(sorted(REDUCE_OPS))}}} | "
            "custom(fn=<callable>, identity=<value>[, tree=false])",
        ),
        _spec("grainsize", "START", _c_grainsize, "<N> | <N>(min_workers=M)"),
        _spec(
            "progress",
            "START",
            _c_progress,
            "true | false | callback(<callable>[, per_task=true, include_result=true])",
        ),
        _spec(
            "affinity",
            "START",
            _c_affinity,
            "compact | scatter | explicit(cores=[...][, numa_node=N])",
        ),
        _spec(
            "args",
            "TRUST",
            _c_args,
            "checked | unchecked | unchecked(only=[name, ...][, skip_runtime_check=true])",
        ),
        _spec(
            "qualname",
            "TRUST",
            _c_qualname,
            "Class.method | Class.method(module=exact.path) | <registry_key>",
        ),
    ]
}


def validate_clause(pragma_kind: str, key: str, cv) -> None:
    if key in _REMOVED_KEYS:
        raise ClauseValueError(f"clause '{key}': {_REMOVED_KEYS[key]}")
    spec = REGISTRY.get(key)
    if spec is None:
        hint = get_close_matches(key, sorted(REGISTRY), n=1)
        suffix = f" - did you mean '{hint[0]}'?" if hint else ""
        raise ClauseValueError(f"unknown clause '{key}'{suffix}")
    if pragma_kind not in spec.hosts:
        raise ClauseValueError(
            f"clause '{key}' is not valid on LUCEN {pragma_kind} "
            f"(valid on: LUCEN {', LUCEN '.join(sorted(spec.hosts))})"
        )
    try:
        spec.check(cv)
    except Invalid as e:
        raise ClauseValueError(
            f"invalid value for '{key}': {e}; accepted forms: {spec.accepts}"
        ) from None
