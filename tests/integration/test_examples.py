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


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_runs_and_is_bit_identical(path):
    plain = _run(path, activate=False)
    activated = _run(path, activate=True)
    assert plain.returncode == 0, plain.stderr
    assert activated.returncode == 0, activated.stderr
    line = _result_line(plain.stdout)
    assert line is not None, plain.stdout
    assert _result_line(activated.stdout) == line
