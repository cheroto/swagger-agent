"""Detect global auth patterns in non-route, non-test files.

Purely deterministic — no LLM calls.
"""

from __future__ import annotations

import os
import re

from swagger_agent.infra.detectors._utils import (
    LANG_EXT_MAP,
    LANG_EXT_FALLBACK,
    glob_files,
    read_file_safe,
)
from swagger_agent.infra.detectors.routes._base import ROUTE_FILE_EXCLUDES


# Auth-related patterns to grep for in non-route files.
# Each pattern is tagged with an auth mode:
#   "all"          — global auth that applies to every endpoint by default
#   "per-endpoint" — auth is opt-in, applied per endpoint via decorators/guards
#   "skip"         — explicit exclusion from auth (modifies "all" mode)
_AUTH_GREP_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Global auth (default = all endpoints require auth)
    (re.compile(r"before_action\s+:auth", re.IGNORECASE), "all"),
    (re.compile(r"before_filter\s+:auth", re.IGNORECASE), "all"),
    (re.compile(r"\.authorizeRequests\b|\.authorizeHttpRequests\b"), "all"),
    # Per-endpoint auth (only marked endpoints require auth)
    (re.compile(r"\[Authorize\]"), "per-endpoint"),
    (re.compile(r"@PreAuthorize\b"), "per-endpoint"),
    (re.compile(r"@UseGuards\b"), "per-endpoint"),
    # Auth infrastructure (could be either — classified as context)
    (re.compile(r"\bmiddleware\b.*\b(auth|jwt|bearer|token)\b", re.IGNORECASE), "all"),
    (re.compile(r"SecuritySchemeType\b"), "per-endpoint"),
    (re.compile(r"passport\.(authenticate|use)\b", re.IGNORECASE), "per-endpoint"),
    # Skip patterns (explicit auth exclusion)
    (re.compile(r"skip_before_action\s+:auth", re.IGNORECASE), "skip"),
    (re.compile(r"AllowAnonymous", re.IGNORECASE), "skip"),
    (re.compile(r"permitAll", re.IGNORECASE), "skip"),
]


def find_auth_context(
    target_dir: str,
    route_files: list[str],
    language: str | None,
) -> tuple[str, str]:
    """Grep for global auth patterns in non-route, non-test files.

    Returns (auth_mode, auth_hint):
      - auth_mode: "all" | "per-endpoint" | "" — deterministic classification
      - auth_hint: human-readable code snippets showing what was found
    """
    glob_pattern = LANG_EXT_MAP.get((language or "").lower()) or LANG_EXT_FALLBACK

    candidates = glob_files(target_dir, glob_pattern)
    route_set = set(route_files)
    auth_hints = ("controller", "middleware", "auth", "security", "guard", "kernel", "concern", "config")

    auth_candidates = []
    other_candidates = []
    for rel_path in candidates:
        if rel_path in route_set:
            continue
        if ROUTE_FILE_EXCLUDES.search(rel_path):
            continue
        base_lower = os.path.basename(rel_path).lower()
        if ".spec." in base_lower or ".test." in base_lower:
            continue
        if any(h in base_lower for h in auth_hints):
            auth_candidates.append(rel_path)
        else:
            other_candidates.append(rel_path)

    search_order = auth_candidates[:30] + other_candidates[:20]

    matches: list[tuple[str, str, str]] = []
    for rel_path in search_order:
        full = os.path.join(target_dir, rel_path)
        content = read_file_safe(full, max_bytes=16_000)
        if not content:
            continue
        for line in content.splitlines():
            for pat, mode_tag in _AUTH_GREP_PATTERNS:
                if pat.search(line):
                    matches.append((rel_path, line.strip(), mode_tag))
                    break
            if len(matches) >= 8:
                break
        if len(matches) >= 8:
            break

    if not matches:
        return "", ""

    mode_tags = {m[2] for m in matches}
    if "all" in mode_tags:
        auth_mode = "all"
    elif "per-endpoint" in mode_tags:
        auth_mode = "per-endpoint"
    else:
        auth_mode = ""

    parts = []
    for fpath, line, _tag in matches[:8]:
        parts.append(f"{fpath}: {line}")
    return auth_mode, "\n".join(parts)
