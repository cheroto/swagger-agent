"""Project-wide HTTP verb sweep — catches routes in unexpected locations.

Greps the entire project for HTTP method + path string patterns,
regardless of framework conventions. This is the safety net for routes
registered in middleware files, auth modules, plugin configs, etc.
"""

from __future__ import annotations

import os
import re

from swagger_agent.infra.detectors._utils import (
    LANG_EXT_MAP,
    LANG_EXT_FALLBACK,
    glob_files,
)
from swagger_agent.infra.detectors.routes._base import ROUTE_FILE_EXCLUDES

# Universal pattern: method call with a path string argument.
# Matches across frameworks: app.post("/login"), router.get("/users"),
# r.POST("/api"), @app.route("/path"), Route::get("/path"), etc.
_HTTP_VERB_PATH_PATTERN = re.compile(
    r"""(?:"""
    r"""\.(get|post|put|patch|delete|head|options)\s*\(\s*["'/]"""  # method chaining: .get("/path"
    r"""|"""
    r"""Route::(get|post|put|patch|delete)\s*\(\s*["'/]"""  # Laravel: Route::get("/path"
    r"""|"""
    r"""@\w+\.(get|post|put|patch|delete|route)\s*\(\s*["'/]"""  # Python decorators: @app.get("/path"
    r""")""",
    re.IGNORECASE,
)


def sweep_for_route_files(
    target_dir: str,
    known_route_files: list[str],
    language: str | None,
) -> list[str]:
    """Find files containing HTTP verb + path patterns not already in known_route_files.

    Returns list of newly discovered route file paths (relative to target_dir).
    """
    glob_pattern = LANG_EXT_MAP.get((language or "").lower()) or LANG_EXT_FALLBACK

    candidates = glob_files(target_dir, glob_pattern)
    known_set = set(known_route_files)
    discovered: list[str] = []

    for rel_path in candidates:
        if rel_path in known_set:
            continue
        if ROUTE_FILE_EXCLUDES.search(rel_path):
            continue
        base_lower = os.path.basename(rel_path).lower()
        if ".spec." in base_lower or ".test." in base_lower:
            continue
        full = os.path.join(target_dir, rel_path)
        try:
            with open(full, "r", errors="ignore") as f:
                for line in f:
                    if _HTTP_VERB_PATH_PATTERN.search(line):
                        discovered.append(rel_path)
                        break
        except OSError:
            continue

    return discovered
