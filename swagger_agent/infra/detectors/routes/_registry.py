"""Route detection registry.

Collects patterns from all language modules and dispatches by framework.
To add patterns for a new framework: add entries to the PATTERNS dict
in the appropriate language module (or create a new one and import here).
"""

from __future__ import annotations

from swagger_agent.infra.detectors.routes._base import RoutePattern, grep_files_matching
from swagger_agent.infra.detectors.routes import (
    javascript,
    python,
    java,
    go,
    ruby,
    rust,
    php,
    csharp,
)

# Merge all per-language pattern dicts into one lookup.
_ALL_PATTERNS: dict[str, list[RoutePattern]] = {}
for _module in (javascript, python, java, go, ruby, rust, php, csharp):
    _ALL_PATTERNS.update(_module.PATTERNS)


def find_route_files(
    target_dir: str,
    framework: str | None,
    language: str | None,
) -> tuple[list[str], list[str]]:
    """Find tentative route files based on framework-specific patterns.

    Returns (route_files, notes).
    """
    if framework is None:
        return [], ["No framework detected, skipping route file detection"]

    patterns = _ALL_PATTERNS.get(framework)
    if patterns is None:
        return [], [f"No route patterns defined for framework '{framework}'"]

    all_files: list[str] = []
    seen: set[str] = set()

    for glob_pat, grep_pat in patterns:
        matches = grep_files_matching(target_dir, glob_pat, grep_pat)
        for f in matches:
            if f not in seen:
                all_files.append(f)
                seen.add(f)

    notes: list[str] = []
    if all_files:
        notes.append(
            f"Found {len(all_files)} tentative route file(s) "
            f"matching {framework} patterns"
        )
    else:
        notes.append(
            f"No files matched {framework} route patterns - "
            f"Scout should verify manually"
        )

    return all_files, notes
