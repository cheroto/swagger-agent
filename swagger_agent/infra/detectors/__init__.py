"""Deterministic detectors for framework, routes, and servers.

Organized as a plugin-style registry so new frameworks can be added
by dropping a file into the appropriate subfolder.
"""

from swagger_agent.infra.detectors.result import PrescanResult, prescan_to_scratchpad
from swagger_agent.infra.detectors.prescan import run_prescan

__all__ = ["PrescanResult", "prescan_to_scratchpad", "run_prescan"]
