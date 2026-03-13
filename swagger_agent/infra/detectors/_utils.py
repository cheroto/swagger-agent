"""Shared file utilities for detectors.

Local copies to avoid circular imports with scout tools.
"""

from __future__ import annotations

import fnmatch
import os
import re

# Directories to skip during file walking (mirrors scout tools)
SKIP_DIRS = frozenset((
    "node_modules", "__pycache__", ".git", "venv", ".venv", "dist", "build",
))


def expand_braces(pattern: str) -> list[str]:
    """Expand brace expressions like *.{js,ts} into [*.js, *.ts]."""
    m = re.search(r"\{([^}]+)\}", pattern)
    if not m:
        return [pattern]
    prefix = pattern[:m.start()]
    suffix = pattern[m.end():]
    alternatives = m.group(1).split(",")
    expanded = []
    for alt in alternatives:
        expanded.extend(expand_braces(prefix + alt.strip() + suffix))
    return expanded


def glob_files(target_dir: str, pattern: str) -> list[str]:
    """Find files matching a glob pattern under target_dir.

    Returns sorted list of relative paths.
    """
    patterns = expand_braces(pattern)
    matches = []
    seen: set[str] = set()
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, target_dir)
            if rel in seen:
                continue
            for p in patterns:
                if fnmatch.fnmatch(rel, p):
                    matches.append(rel)
                    seen.add(rel)
                    break
    matches.sort()
    return matches


def read_file_safe(path: str, max_bytes: int = 64_000) -> str:
    """Read a file, returning '' on any error."""
    try:
        with open(path, "r", errors="ignore") as f:
            return f.read(max_bytes)
    except OSError:
        return ""
