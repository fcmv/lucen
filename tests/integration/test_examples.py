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


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_runs_and_is_bit_identical(path):
    plain = _run(path, activate=False)
    activated = _run(path, activate=True)
    assert plain.returncode == 0, plain.stderr
    assert activated.returncode == 0, activated.stderr
    line = _result_line(plain.stdout)
    assert line is not None, plain.stdout
    assert _result_line(activated.stdout) == line
