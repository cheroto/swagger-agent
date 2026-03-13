"""Deterministic pre-scan orchestrator.

Chains framework detection -> route detection -> server detection.
No LLM calls. The output seeds the Scout's initial state.
"""

from __future__ import annotations

import logging
import os

from swagger_agent.infra.detectors.result import PrescanResult
from swagger_agent.infra.detectors.framework import detect_framework
from swagger_agent.infra.detectors.routes import find_route_files
from swagger_agent.infra.detectors.servers import find_servers

logger = logging.getLogger("swagger_agent.prescan")


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

    # 3. Find servers and base path
    servers, base_path, server_notes = find_servers(target_dir, fw, lang)
    result.servers = servers
    result.base_path = base_path
    result.notes.extend(server_notes)

    logger.info(
        "Prescan complete: framework=%s, language=%s, %d route(s), %d server(s)",
        fw, lang, len(routes), len(servers),
    )

    return result
