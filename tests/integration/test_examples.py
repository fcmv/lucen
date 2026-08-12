import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
EXAMPLES = sorted(EXAMPLES_DIR.glob("*.py"))


def _run(path, activate):
    prelude = "import lucen; lucen.activate(); " if activate else ""
    code = f"{prelude}import runpy; runpy.run_path({str(path)!r}, run_name='__main__')"
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


def _result_line(out):
    for line in out.splitlines():
        if line.startswith(("checksum:", "results[-1]")):
            return line
    return None


def test_lucen_run_reports_the_script_as_the_entry_module(tmp_path):
    # The spawn-safety scan reads sys.modules["__main__"].__file__; if that is
    # Lucen's own launcher it looks for the guard in the wrong file and refuses
    # the process backend on every `lucen run`.
    script = tmp_path / "probe.py"
    script.write_text(
        "import sys\nprint(sys.modules['__main__'].__file__)\n"
        'if __name__ == "__main__":\n    pass\n',
        encoding="utf-8",
    )
    done = subprocess.run(
        [sys.executable, "-m", "lucen", "run", str(script)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == str(script)


COMPREHENSION_SCRIPT = """
import math


def score(x):
    acc = 0.0
    for k in range(200):
        acc += math.sin(x * 0.001 + k) * math.cos(k * 0.5)
    return acc


def main():
    records = list(range(3000))
    # LUCEN START
    scores = [score(r) for r in records]
    # LUCEN END
    print(f"checksum: {sum(scores):.10f}")
    print(f"leaked: {'r' in dir()}")


if __name__ == "__main__":
    main()
"""


ALL_COMPREHENSIONS_SCRIPT = """
import math


def f(x):
    acc = 0.0
    for k in range(120):
        acc += math.sin(x * 0.001 + k) * math.cos(k * 0.5)
    return acc


def key(x):
    return "k%d" % (x % 97)


def keep(x):
    return x % 3 != 0


def main():
    xs = list(range(600))
    ys = [1, 2, 3]
    # LUCEN START
    a = [f(x) for x in xs]
    # LUCEN END
    # LUCEN START
    b = [f(x) for x in xs if keep(x)]
    # LUCEN END
    # LUCEN START
    c = {key(x): f(x) for x in xs}
    # LUCEN END
    # LUCEN START
    d = {round(f(x), 6) for x in xs}
    # LUCEN END
    # LUCEN START
    e = [x * y for x in xs for y in ys]
    # LUCEN END
    # LUCEN START
    g = {key(x): f(x) for x in xs if keep(x)}
    # LUCEN END
    # LUCEN START
    h = sum(f(x) for x in xs)
    # LUCEN END
    # LUCEN START
    i = sum((f(x) for x in xs if keep(x)), 5.25)
    # LUCEN END
    print("checksum:", len(a), len(b), len(c), len(d), len(e), len(g))
    print("order:", list(c)[:4], list(g)[:4], sorted(d)[:2])
    print("reduced:", repr(h), repr(i))
    print("sums:", "%.10f" % sum(a), "%.10f" % sum(b), sum(e))
    print("leaked:", [n for n in ("x", "y") if n in dir()])


if __name__ == "__main__":
    main()
"""


def test_every_comprehension_form_is_bit_identical(tmp_path):
    # Dict and set results are rebuilt from ordered slots, so insertion order
    # (and therefore iteration order) has to match plain Python too.
    script = tmp_path / "allcomp.py"
    script.write_text(ALL_COMPREHENSIONS_SCRIPT, encoding="utf-8")
    plain = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    lucen = subprocess.run(
        [sys.executable, "-m", "lucen", "run", str(script)], capture_output=True, text=True
    )
    assert plain.returncode == 0, plain.stderr
    assert lucen.returncode == 0, lucen.stderr
    assert lucen.stdout == plain.stdout


COMPENSATED_SUM_SCRIPT = """
def f(x):
    return 1e16 if x == 0 else 1.0


def main():
    xs = list(range(400))
    # LUCEN START
    total = sum(f(x) for x in xs)
    # LUCEN END
    # LUCEN START
    kept = sum((f(x) for x in xs if x != 7), 1e16)
    # LUCEN END
    print("sums:", repr(total), repr(kept))


if __name__ == "__main__":
    main()
"""


def test_sum_reproduces_the_builtin_not_an_accumulating_loop(tmp_path):
    # `sum` is not a `+=` loop on CPython 3.12 and newer, which compensate float
    # sums. These magnitudes make the two disagree on any IEEE-754 platform; a
    # data-dependent checksum only catches it where libm rounds it into view.
    script = tmp_path / "compsum.py"
    script.write_text(COMPENSATED_SUM_SCRIPT, encoding="utf-8")
    plain = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    lucen = subprocess.run(
        [sys.executable, "-m", "lucen", "run", str(script)], capture_output=True, text=True
    )
    assert plain.returncode == 0, plain.stderr
    assert lucen.returncode == 0, lucen.stderr
    assert lucen.stdout == plain.stdout


def test_list_comprehension_is_bit_identical(tmp_path):
    # A comprehension desugars to an indexed loop, so the result must match
    # plain Python exactly and the comprehension variable must not leak.
    script = tmp_path / "comp.py"
    script.write_text(COMPREHENSION_SCRIPT, encoding="utf-8")
    plain = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    lucen = subprocess.run(
        [sys.executable, "-m", "lucen", "run", str(script)], capture_output=True, text=True
    )
    assert plain.returncode == 0, plain.stderr
    assert lucen.returncode == 0, lucen.stderr
    assert _result_line(lucen.stdout) == _result_line(plain.stdout)
    assert "leaked: False" in lucen.stdout


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_runs_and_is_bit_identical(path):
    plain = _run(path, activate=False)
    activated = _run(path, activate=True)
    assert plain.returncode == 0, plain.stderr
    assert activated.returncode == 0, activated.stderr
    line = _result_line(plain.stdout)
    assert line is not None, plain.stdout
    assert _result_line(activated.stdout) == line
