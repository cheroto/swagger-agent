"""Deterministic pre-scan orchestrator.

Chains framework detection -> route detection -> importer detection ->
HTTP verb sweep -> auth context -> server detection.
No LLM calls. The output seeds the Scout's initial state.
"""

from __future__ import annotations

import logging

from swagger_agent.infra.detectors.result import PrescanResult
from swagger_agent.infra.detectors.framework import detect_framework
from swagger_agent.infra.detectors.routes import find_route_files
from swagger_agent.infra.detectors.servers import find_servers
from swagger_agent.infra.detectors.importers import find_importers
from swagger_agent.infra.detectors.auth import find_auth_context
from swagger_agent.infra.detectors.verb_sweep import sweep_for_route_files

logger = logging.getLogger("swagger_agent.prescan")


def run_prescan(target_dir: str) -> PrescanResult:
    """Run a deterministic pre-scan of the target directory.

    Returns a PrescanResult with tentative findings. All results are
    heuristic-based and should be validated by the Scout agent.
    """
    import os
    target_dir = os.path.normpath(os.path.abspath(target_dir))
    result = PrescanResult()

    # 1. Detect framework and language
    fw, lang, fw_notes = detect_framework(target_dir)
    result.framework = fw
    result.language = lang
    result.notes.extend(fw_notes)

    # 2. Find route files (framework-specific patterns)
    routes, route_notes = find_route_files(target_dir, fw, lang)
    result.route_files = routes
    result.notes.extend(route_notes)

    # 3. Find files that import route files (registry/mount files)
    importers = find_importers(target_dir, routes, lang)
    if importers:
        result.route_files = importers + routes
        result.notes.append(
            f"Added {len(importers)} importer file(s): {', '.join(importers)}"
        )
        logger.info("Found %d importer(s) of route files: %s", len(importers), importers)

    # 4. Project-wide HTTP verb sweep — catches routes in unexpected locations
    #    (middleware files, auth modules, plugin configs, etc.)
    sweep_hits = sweep_for_route_files(target_dir, result.route_files, lang)
    if sweep_hits:
        result.route_files.extend(sweep_hits)
        result.notes.append(
            f"HTTP verb sweep found {len(sweep_hits)} additional file(s): "
            f"{', '.join(sweep_hits)}"
        )
        logger.info("Verb sweep found %d extra route file(s): %s", len(sweep_hits), sweep_hits)

    # 5. Find global auth context
    auth_mode, auth_hint = find_auth_context(target_dir, result.route_files, lang)
    if auth_hint:
        result.auth_mode = auth_mode
        result.auth_context_hint = auth_hint
        result.notes.append(f"Auth mode='{auth_mode}' from {auth_hint.splitlines()[0].split(':')[0]}")
        logger.info("Auth context: mode=%s, first match: %s", auth_mode, auth_hint.splitlines()[0])

    # 6. Find servers and base path
    servers, base_path, server_notes = find_servers(target_dir, fw, lang)
    result.servers = servers
    result.base_path = base_path
    result.notes.extend(server_notes)

    logger.info(
        "Prescan complete: framework=%s, language=%s, %d route(s), %d server(s)",
        fw, lang, len(result.route_files), len(servers),
    )

    return result
