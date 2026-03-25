"""Find files that import/require/include detected route files.

These are typically registry files that mount sub-routers at path prefixes.
"""

from __future__ import annotations

import os
import re

from swagger_agent.infra.detectors._utils import (
    LANG_EXT_MAP,
    glob_files,
)
from swagger_agent.infra.detectors.routes._base import ROUTE_FILE_EXCLUDES


def find_importers(
    target_dir: str,
    route_files: list[str],
    language: str | None,
) -> list[str]:
    """Find files that import/require/include any detected route file.

    These are likely registry files that mount sub-routers at path prefixes.
    Uses a simple approach: grep the project for each route file's
    basename/module name. Returns files not already in route_files.
    """
    if not route_files:
        return []

    # Build search terms from route file paths.
    search_terms: set[str] = set()
    for rf in route_files:
        basename = os.path.basename(rf)
        stem = os.path.splitext(basename)[0]
        search_terms.add(basename)
        if "." in stem:
            search_terms.add(stem)
        parent = os.path.basename(os.path.dirname(rf))
        if parent:
            search_terms.add(f"{parent}/{stem}")
        if rf.endswith(".go") and parent:
            search_terms.add(parent)

    # Skip short terms that match too broadly
    search_terms = {t for t in search_terms if len(t) > 5}

    escaped = [re.escape(t) for t in sorted(search_terms)]
    if not escaped:
        return []
    combined = re.compile(r"""(?:["'/])(?:\.*/)*(?:""" + "|".join(escaped) + r")")

    glob_pattern = LANG_EXT_MAP.get((language or "").lower(), "*")

    candidates = glob_files(target_dir, glob_pattern)
    route_set = set(route_files)
    importers: list[str] = []

    for rel_path in candidates:
        if rel_path in route_set:
            continue
        if ROUTE_FILE_EXCLUDES.search(rel_path):
            continue
        base_lower = os.path.basename(rel_path).lower()
        if ".spec." in base_lower or ".test." in base_lower:
            continue
        if base_lower.endswith(".module.ts") or base_lower.endswith(".module.js"):
            continue
        full = os.path.join(target_dir, rel_path)
        try:
            with open(full, "r", errors="ignore") as f:
                content = f.read(64_000)
            if combined.search(content):
                importers.append(rel_path)
        except OSError:
            continue

    return importers
