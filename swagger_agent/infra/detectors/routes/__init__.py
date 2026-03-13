"""Route file detection subsystem.

Each framework family has a module defining glob+grep pattern pairs.
The registry dispatches by detected framework name.
"""

from swagger_agent.infra.detectors.routes._registry import find_route_files

__all__ = ["find_route_files"]
