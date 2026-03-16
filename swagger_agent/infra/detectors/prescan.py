"""Deterministic pre-scan orchestrator.

Chains framework detection -> route detection -> importer detection -> server detection.
No LLM calls. The output seeds the Scout's initial state.
"""

from __future__ import annotations

import logging
import os
import re

from swagger_agent.infra.detectors.result import PrescanResult
from swagger_agent.infra.detectors.framework import detect_framework
from swagger_agent.infra.detectors.routes import find_route_files
from swagger_agent.infra.detectors.routes._base import ROUTE_FILE_EXCLUDES
from swagger_agent.infra.detectors.servers import find_servers
from swagger_agent.infra.detectors._utils import glob_files

logger = logging.getLogger("swagger_agent.prescan")


def _find_importers(
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
    # We need terms specific enough to appear only in import/mount statements,
    # not in general code references.
    # e.g. "src/routes/v1/auth.route.js" -> "auth.route.js", "auth.route"
    # e.g. "controllers/users.go" -> "controllers/users"
    search_terms: set[str] = set()
    for rf in route_files:
        basename = os.path.basename(rf)
        stem = os.path.splitext(basename)[0]
        # Full basename with extension (require('./auth.route.js'))
        search_terms.add(basename)
        # Stem with dot (require('./auth.route')) — only if compound name
        if "." in stem:
            search_terms.add(stem)
        # Parent/stem path (import "controllers/users", from routes.auth)
        parent = os.path.basename(os.path.dirname(rf))
        if parent:
            search_terms.add(f"{parent}/{stem}")
        # Go package imports use the package directory name, not individual files.
        # e.g. import "github.com/user/project/controllers" → search for package name
        if rf.endswith(".go") and parent:
            search_terms.add(parent)

    # Skip short terms that match too broadly
    search_terms = {t for t in search_terms if len(t) > 5}

    # Build regex — require term in a quoted string or after path separator
    escaped = [re.escape(t) for t in sorted(search_terms)]
    if not escaped:
        return []
    combined = re.compile(r"""(?:["'/])(?:\.*/)*(?:""" + "|".join(escaped) + r")")

    # Determine which source file extensions to search
    ext_map = {
        "javascript": "*.{js,ts,mjs,cjs}",
        "typescript": "*.{js,ts,mjs,cjs}",
        "python": "*.py",
        "go": "*.go",
        "ruby": "*.rb",
        "rust": "*.rs",
        "php": "*.php",
        "java": "*.{java,kt}",
        "kotlin": "*.{java,kt}",
        "csharp": "*.cs",
        "swift": "*.swift",
        "dart": "*.dart",
        "haskell": "*.hs",
        "ocaml": "*.{ml,mli}",
        "clojure": "*.{clj,cljs,cljc}",
    }
    glob_pattern = ext_map.get((language or "").lower(), "*")

    candidates = glob_files(target_dir, glob_pattern)
    route_set = set(route_files)
    importers: list[str] = []

    for rel_path in candidates:
        if rel_path in route_set:
            continue
        if ROUTE_FILE_EXCLUDES.search(rel_path):
            continue
        # Skip test files and DI/module wiring files (not route registries)
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


def run_prescan(target_dir: str) -> PrescanResult:
    """Run a deterministic pre-scan of the target directory.

    Returns a PrescanResult with tentative findings. All results are
    heuristic-based and should be validated by the Scout agent.
    """
    target_dir = os.path.normpath(os.path.abspath(target_dir))
    result = PrescanResult()

    # 1. Detect framework and language
    fw, lang, fw_notes = detect_framework(target_dir)
    result.framework = fw
    result.language = lang
    result.notes.extend(fw_notes)

    # 2. Find route files
    routes, route_notes = find_route_files(target_dir, fw, lang)
    result.route_files = routes
    result.notes.extend(route_notes)

    # 3. Find files that import route files (registry/mount files)
    importers = _find_importers(target_dir, routes, lang)
    if importers:
        result.route_files = importers + routes  # importers first so mount_map is collected before dependents
        result.notes.append(
            f"Added {len(importers)} importer file(s): {', '.join(importers)}"
        )
        logger.info("Found %d importer(s) of route files: %s", len(importers), importers)

    # 4. Find servers and base path
    servers, base_path, server_notes = find_servers(target_dir, fw, lang)
    result.servers = servers
    result.base_path = base_path
    result.notes.extend(server_notes)

    logger.info(
        "Prescan complete: framework=%s, language=%s, %d route(s), %d server(s)",
        fw, lang, len(routes) + len(importers), len(servers),
    )

    return result
