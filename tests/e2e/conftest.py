"""E2E test fixtures.

These tests require:
- A running LLM server (configured via LLMConfig / env vars)
- The test repos at SWAGGER_AGENT_REPOS_ROOT
- universal-ctags installed (for schema loop tests)
"""

from __future__ import annotations

import pytest


e2e = pytest.mark.e2e
