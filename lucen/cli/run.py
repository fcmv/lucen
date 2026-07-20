from __future__ import annotations

import sys
from typing import List


def run(script: str, argv: List[str]) -> int:
    """Run a script with Lucen active, rewriting the script itself.

    Plain `python script.py` cannot parallelize a marked loop in the entry
    script: by the time `lucen.activate()` installs the import hook, the entry
    module is already compiled and running. This runs the script through the
    rewriter first, so the marked loops in the file you point at are executed
    in parallel, no separate importable module required.
    """
    import lucen
    from lucen import import_hook

    old_argv = sys.argv
    sys.argv = [script] + list(argv)
    lucen.activate()
    try:
        import_hook.run_path(script, run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = old_argv
    return 0
