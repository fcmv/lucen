"""Print the CHANGELOG section for one version, for use as release notes.

    python tools/release_notes.py 1.1.0

Exits non-zero if the version has no section, so a release cannot publish with
empty or wrong notes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def section(changelog: str, version: str) -> str:
    lines = changelog.splitlines()
    heading = re.compile(rf"^## \[{re.escape(version)}\]")
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1 : end]).strip()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    version = sys.argv[1].lstrip("v")
    path = Path("CHANGELOG.md")
    if not path.is_file():
        print("error: CHANGELOG.md not found; run from the repository root", file=sys.stderr)
        return 1
    body = section(path.read_text(encoding="utf-8"), version)
    if not body:
        print(f"error: CHANGELOG.md has no '## [{version}]' section", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
