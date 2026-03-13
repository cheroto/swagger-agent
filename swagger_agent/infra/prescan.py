"""Backward-compatible re-exports from the detectors package.

The actual implementation now lives in swagger_agent.infra.detectors/.
This shim keeps existing imports working:
    from swagger_agent.infra.prescan import run_prescan, PrescanResult, prescan_to_scratchpad
"""

from swagger_agent.infra.detectors.result import PrescanResult, prescan_to_scratchpad
from swagger_agent.infra.detectors.prescan import run_prescan

__all__ = ["PrescanResult", "prescan_to_scratchpad", "run_prescan"]
