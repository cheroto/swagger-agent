"""Framework detection subsystem.

Each language has its own detector module. The registry chains them
in priority order and returns the first match.
"""

from swagger_agent.infra.detectors.framework._registry import detect_framework

__all__ = ["detect_framework"]
