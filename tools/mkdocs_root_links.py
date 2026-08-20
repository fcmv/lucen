"""Point the links inside root-level guides at their site pages.

The guides under `docs/` that pull in a root file with `include-markdown` carry
that file's own relative links, which are written for a reader on GitHub. On
the site those either resolve outside `docs/` or not at all, so this rewrites
them: documents the site publishes become their site page, and the rest become
absolute GitHub links.

Registered as a `hooks:` entry in mkdocs.yml.
"""

from __future__ import annotations

import re

REPO_BLOB = "https://github.com/fcmv/lucen/blob/main"

# Stub pages, keyed by their path under docs/, mapped to the root file each one
# includes. Only these pages are rewritten; every other page is authored
# against the site layout already.
INCLUDED_PAGES = {
    "limitations.md": "LIMITATIONS.md",
    "roadmap.md": "ROADMAP.md",
    "stability.md": "STABILITY.md",
    "benchmark.md": "BENCHMARK.md",
    "changelog.md": "CHANGELOG.md",
    "support.md": "SUPPORT.md",
    "contributing.md": "CONTRIBUTING.md",
}

# Root documents the site publishes, mapped to the page that publishes them.
ON_SITE = {source: page for page, source in INCLUDED_PAGES.items()}
ON_SITE["README.md"] = "index.md"

# Root paths the site does not publish, which have to leave for GitHub.
OFF_SITE = (
    "AI_USAGE_GUIDELINE_FOR_PR.md",
    "CODE_OF_CONDUCT.md",
    "GOVERNANCE.md",
    "RELEASING.md",
    "SECURITY.md",
    "LICENSE",
    "NOTICE",
    "examples/",
    "tests/",
)

_LINK_RE = re.compile(r"\]\((?!https?:|#|mailto:)(?P<target>[^)]+)\)")


def _retarget(target: str) -> str:
    path, sep, anchor = target.partition("#")
    # rewrite-relative-urls has already made every path relative to docs/.
    path = path[3:] if path.startswith("../") else path

    if path in ON_SITE:
        return ON_SITE[path] + sep + anchor
    if path.startswith("docs/"):
        return path[len("docs/") :] + sep + anchor
    if path.startswith(OFF_SITE):
        return f"{REPO_BLOB}/{path}{sep}{anchor}"
    return target


def on_page_markdown(markdown: str, page, config, files) -> str:
    if page.file.src_uri not in INCLUDED_PAGES:
        return markdown
    return _LINK_RE.sub(lambda m: "](" + _retarget(m.group("target")) + ")", markdown)
