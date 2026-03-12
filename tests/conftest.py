"""Shared test configuration."""

from __future__ import annotations

import os

import pytest

from swagger_agent.config import LLMConfig

REPOS_ROOT = os.environ.get(
    "SWAGGER_AGENT_REPOS_ROOT",
    "/Users/cheroto/Code/work-projects/agentic-flows/rl-experiment/repos",
)


@pytest.fixture(scope="session")
def repos_root() -> str:
    if not os.path.isdir(REPOS_ROOT):
        pytest.skip(f"Repos root not found: {REPOS_ROOT}")
    return REPOS_ROOT


@pytest.fixture(scope="session")
def llm_config() -> LLMConfig:
    return LLMConfig()
