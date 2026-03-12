"""Shared test configuration — clones test repos on demand from manifest."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import pytest

from swagger_agent.config import LLMConfig

logger = logging.getLogger("swagger_agent.tests")

_MANIFEST_PATH = Path(__file__).parent / "e2e" / "repos.json"
_DEFAULT_REPOS_DIR = Path(__file__).parent / "e2e" / "repos"


def _ensure_repos(repos_dir: Path) -> None:
    """Clone/checkout repos listed in repos.json if missing or stale."""
    if not _MANIFEST_PATH.is_file():
        return

    with open(_MANIFEST_PATH) as f:
        manifest = json.load(f)

    repos_dir.mkdir(parents=True, exist_ok=True)

    for repo_id, info in manifest["repos"].items():
        repo_path = repos_dir / repo_id
        url = info["url"]
        commit = info["commit"]

        if repo_path.is_dir():
            # Already cloned — verify we're on the right commit
            try:
                head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(repo_path),
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                if head == commit:
                    continue
                # Wrong commit — checkout the right one
                logger.info("Checking out %s @ %s", repo_id, commit[:10])
                subprocess.run(
                    ["git", "checkout", commit],
                    cwd=str(repo_path),
                    capture_output=True, timeout=30,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass
            continue

        # Clone fresh
        logger.info("Cloning %s from %s", repo_id, url)
        try:
            subprocess.run(
                ["git", "clone", "--quiet", url, str(repo_path)],
                capture_output=True, timeout=120,
                check=True,
            )
            subprocess.run(
                ["git", "checkout", "--quiet", commit],
                cwd=str(repo_path),
                capture_output=True, timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning("Failed to clone %s: %s", repo_id, e)


@pytest.fixture(scope="session")
def repos_root() -> str:
    """Return path to test repos, cloning from manifest if needed.

    Override with SWAGGER_AGENT_REPOS_ROOT env var to use a custom location.
    """
    override = os.environ.get("SWAGGER_AGENT_REPOS_ROOT")
    if override:
        if not os.path.isdir(override):
            pytest.skip(f"Repos root not found: {override}")
        return override

    repos_dir = _DEFAULT_REPOS_DIR
    _ensure_repos(repos_dir)

    if not repos_dir.is_dir():
        pytest.skip(f"Repos dir not found and could not be created: {repos_dir}")
    return str(repos_dir)


@pytest.fixture(scope="session")
def llm_config() -> LLMConfig:
    return LLMConfig()
