"""Base types and shared logic for route detection."""

from __future__ import annotations

import os
import re

from swagger_agent.infra.detectors._utils import glob_files

# (glob_pattern, grep_regex)
RoutePattern = tuple[str, str]

# File patterns that should be excluded from route detection
ROUTE_FILE_EXCLUDES = re.compile(
    r"(^|/)(test[s_]?/|__test__|spec/|\.test\.|\.spec\.|test_|_test\.|"
    r"migrations?/|db/migrate/|seeds?/|fixtures?/|"
    r"node_modules/|vendor/|dist/|build/|"
    r"conftest\.py$|setup\.py$|manage\.py$|wsgi\.py$|asgi\.py$|"
    r"webpack|jest|babel|eslint|prettier|tsconfig|"
    r"\.d\.ts$|\.min\.)"
)


def grep_files_matching(
    target_dir: str,
    glob_pattern: str,
    grep_pattern: str,
) -> list[str]:
    """Find files matching glob_pattern that contain grep_pattern.

    Returns deduplicated list of relative file paths. No match cap.
    """
    files = glob_files(target_dir, glob_pattern)
    try:
        compiled = re.compile(grep_pattern)
    except re.error:
        return []

    matching: list[str] = []
    seen: set[str] = set()
    for rel_path in files:
        if rel_path in seen:
            continue
        if ROUTE_FILE_EXCLUDES.search(rel_path):
            continue
        full = os.path.join(target_dir, rel_path)
        try:
            with open(full, "r", errors="ignore") as f:
                for line in f:
                    if compiled.search(line):
                        matching.append(rel_path)
                        seen.add(rel_path)
                        break
        except OSError:
            continue

    return matching
