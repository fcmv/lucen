from __future__ import annotations

import copy
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lucen.execution import dispatch
from lucen.import_hook import rewrite_module
from lucen.support import config


@pytest.fixture(autouse=True)
def _clean_state():
    config.set_active(config.Config())
    dispatch.reset_runtime_state()
    yield
    dispatch.reset_runtime_state()
    config.set_active(config.Config())


def bit_equal(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return (math.isnan(a) and math.isnan(b)) or repr(a) == repr(b)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(bit_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _run_lucen(src, base):
    entry = rewrite_module(src, "gen.py")
    ns = copy.deepcopy(base)
    if entry.rewritten is None:
        exec(compile(src, "gen.py", "exec"), ns)
        return ns
    ns["_lucen_rt"] = dispatch
    for line, spec in entry.specs:
        ns[f"_PLX_SPEC_{line}"] = spec
    exec(compile(entry.rewritten, "gen.py", "exec"), ns)
    return ns


def _expr(depth):
    atom = st.one_of(st.just("xs[i]"), st.just("i"), st.integers(-5, 5).map(str))
    if depth <= 0:
        return atom
    sub = _expr(depth - 1)
    return st.one_of(
        atom,
        st.builds(lambda a, o, b: f"({a} {o} {b})", sub, st.sampled_from(["+", "-", "*"]), sub),
    )


def _wrap(header, body):
    lines = "\n".join("    " + line for line in body)
    return f"# LUCEN START backend=thread, calibrate=false\n{header}\n{lines}\n# LUCEN END\n"


@st.composite
def _program(draw):
    n = draw(st.integers(min_value=2, max_value=150))
    use_float = draw(st.booleans())
    if use_float:
        xs = draw(
            st.lists(
                st.floats(allow_nan=False, allow_infinity=False, min_value=-1e3, max_value=1e3),
                min_size=n,
                max_size=n,
            )
        )
        zero = 0.0
    else:
        xs = draw(st.lists(st.integers(-100, 100), min_size=n, max_size=n))
        zero = 0

    base = {"xs": xs}
    result_keys = []
    parts = []
    header = "for i in range(len(xs)):"
    for b in range(draw(st.integers(min_value=1, max_value=3))):
        kind = draw(st.sampled_from(["map", "reduction", "branch", "inner_loop", "chain"]))
        e = draw(_expr(2))
        out = f"o{b}"
        if kind == "reduction":
            acc = f"a{b}"
            base[acc] = zero
            result_keys.append(acc)
            parts.append(_wrap(header, [f"{acc} += {e}"]))
            continue
        base[out] = [zero] * n
        result_keys.append(out)
        if kind == "map":
            parts.append(_wrap(header, [f"{out}[i] = {e}"]))
        elif kind == "branch":
            e2 = draw(_expr(1))
            parts.append(
                _wrap(
                    header,
                    ["if xs[i] > 0:", f"    {out}[i] = {e}", "else:", f"    {out}[i] = {e2}"],
                )
            )
        elif kind == "inner_loop":
            parts.append(
                _wrap(
                    header,
                    [
                        "s = 0",
                        "for k in range(3):",
                        "    s = s + xs[i] * k",
                        f"{out}[i] = s + ({e})",
                    ],
                )
            )
        else:
            prev = next((k for k in reversed(result_keys[:-1]) if k.startswith("o")), None)
            read = f"{prev}[i]" if prev else "xs[i]"
            parts.append(_wrap(header, [f"{out}[i] = {read} + ({e})"]))

    return "".join(parts), base, result_keys


@given(program=_program())
def test_generated_program_is_bit_identical(program):
    src, base, keys = program
    plain = copy.deepcopy(base)
    exec(compile(src, "gen.py", "exec"), plain)
    parallel = _run_lucen(src, base)
    for key in keys:
        assert bit_equal(parallel[key], plain[key]), (key, src)
