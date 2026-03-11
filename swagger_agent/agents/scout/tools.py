"""Scout agent tool implementations.

These tools are sandboxed to the target directory. The Scout cannot
read full files — only heads and ranges — to stay within context budget.
"""

from __future__ import annotations

import fnmatch
import os
import re

from swagger_agent.tools import Tool


def _resolve_path(target_dir: str, path: str) -> str:
    """Resolve a path relative to target_dir, preventing directory traversal."""
    resolved = os.path.normpath(os.path.join(target_dir, path))
    if not resolved.startswith(os.path.normpath(target_dir)):
        raise ValueError(f"Path traversal blocked: {path}")
    return resolved


def _expand_braces(pattern: str) -> list[str]:
    """Expand brace expressions like *.{js,ts} into [*.js, *.ts].

    fnmatch doesn't support brace expansion, so we handle it ourselves.
    Supports one level of braces only (no nesting).
    """
    import re as _re
    m = _re.search(r"\{([^}]+)\}", pattern)
    if not m:
        return [pattern]
    prefix = pattern[:m.start()]
    suffix = pattern[m.end():]
    alternatives = m.group(1).split(",")
    expanded = []
    for alt in alternatives:
        expanded.extend(_expand_braces(prefix + alt.strip() + suffix))
    return expanded


def _glob_impl(target_dir: str, pattern: str) -> list[str]:
    """Find files matching a glob pattern under target_dir.

    Supports brace expansion (e.g. **/*.{js,ts}).
    """
    patterns = _expand_braces(pattern)
    matches = []
    seen = set()
    for root, _dirs, files in os.walk(target_dir):
        # Skip hidden dirs and common non-source dirs
        _dirs[:] = [
            d for d in _dirs
            if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git", "venv", ".venv", "dist", "build")
        ]
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


def _grep_impl(target_dir: str, pattern: str, path: str) -> list[dict]:
    """Search for a regex pattern in files under path (relative to target_dir).

    Returns list of {file, line_number, line} dicts. Caps at 50 matches.
    """
    search_root = _resolve_path(target_dir, path)
    try:
        compiled = re.compile(pattern)
    except re.error:
        return [{"error": f"Invalid regex: {pattern}"}]

    matches = []
    if os.path.isfile(search_root):
        files_to_search = [search_root]
    else:
        files_to_search = []
        for root, _dirs, files in os.walk(search_root):
            _dirs[:] = [
                d for d in _dirs
                if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git", "venv", ".venv", "dist", "build")
            ]
            for fname in files:
                files_to_search.append(os.path.join(root, fname))

    for fpath in files_to_search:
        try:
            with open(fpath, "r", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if compiled.search(line):
                        rel = os.path.relpath(fpath, target_dir)
                        matches.append({
                            "file": rel,
                            "line_number": i,
                            "line": line.rstrip()[:200],
                        })
                        if len(matches) >= 50:
                            return matches
        except (OSError, UnicodeDecodeError):
            continue

    return matches


def _read_file_head_impl(target_dir: str, path: str, n_lines: int) -> str:
    """Read first n_lines of a file."""
    resolved = _resolve_path(target_dir, path)
    n_lines = min(n_lines, 100)  # Cap to prevent context blowout
    lines = []
    try:
        with open(resolved, "r", errors="ignore") as f:
            for i, line in enumerate(f, 1):
                if i > n_lines:
                    break
                lines.append(f"{i}: {line.rstrip()}")
    except OSError as e:
        return f"Error reading {path}: {e}"
    return "\n".join(lines)


def _read_file_range_impl(target_dir: str, path: str, start: int, end: int) -> str:
    """Read lines [start, end] (1-indexed, inclusive) of a file."""
    resolved = _resolve_path(target_dir, path)
    # Cap range to 100 lines
    if end - start + 1 > 100:
        end = start + 99
    lines = []
    try:
        with open(resolved, "r", errors="ignore") as f:
            for i, line in enumerate(f, 1):
                if i > end:
                    break
                if i >= start:
                    lines.append(f"{i}: {line.rstrip()}")
    except OSError as e:
        return f"Error reading {path}: {e}"
    return "\n".join(lines)


def build_scout_tools(target_dir: str) -> dict[str, Tool]:
    """Build the Scout's tool set, bound to a specific target directory."""
    return {
        "glob": Tool(
            name="glob",
            description="Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). Returns relative paths.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern to match files (e.g. '**/*.py', 'app/**/*.java')",
                    },
                },
                "required": ["pattern"],
            },
            fn=lambda pattern: _glob_impl(target_dir, pattern),
        ),
        "grep": Tool(
            name="grep",
            description="Search for a regex pattern in files. Returns up to 50 matches with file, line number, and line content.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for (e.g. '@app\\.route', 'class.*BaseModel')",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search in, relative to project root (e.g. '.' for all, 'app/routes')",
                    },
                },
                "required": ["pattern", "path"],
            },
            fn=lambda pattern, path: _grep_impl(target_dir, pattern, path),
        ),
        "read_file_head": Tool(
            name="read_file_head",
            description="Read the first N lines of a file. Use for imports, decorators, config sections. Max 100 lines.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to project root",
                    },
                    "n_lines": {
                        "type": "integer",
                        "description": "Number of lines to read from the start (max 100)",
                    },
                },
                "required": ["path", "n_lines"],
            },
            fn=lambda path, n_lines: _read_file_head_impl(target_dir, path, n_lines),
        ),
        "read_file_range": Tool(
            name="read_file_range",
            description="Read a specific line range [start, end] (1-indexed, inclusive). Max 100 lines per call.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to project root",
                    },
                    "start": {
                        "type": "integer",
                        "description": "Starting line number (1-indexed)",
                    },
                    "end": {
                        "type": "integer",
                        "description": "Ending line number (inclusive)",
                    },
                },
                "required": ["path", "start", "end"],
            },
            fn=lambda path, start, end: _read_file_range_impl(target_dir, path, start, end),
        ),
        # write_artifact is intercepted by the harness.
        "write_artifact": Tool(
            name="write_artifact",
            description=(
                "Write the final discovery manifest. Call this when all remaining_tasks are complete. "
                "The data should contain all accumulated findings."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "artifact_type": {
                        "type": "string",
                        "enum": ["discovery_manifest"],
                    },
                    "data": {
                        "type": "object",
                        "description": "The discovery manifest data",
                    },
                },
                "required": ["artifact_type", "data"],
            },
            fn=lambda artifact_type, data: data,  # Intercepted by harness
        ),
    }
