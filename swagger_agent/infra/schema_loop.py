"""Schema loop — backward-compatible re-export shim.

The schema loop has been split into:
  - schema_loop_pkg/type_hints.py — type hint decomposition (builtins, generics, unions)
  - schema_loop_pkg/loop.py       — resolution loop, CLI, ref_hint collection
"""

from swagger_agent.infra.schema_loop_pkg import *  # noqa: F401, F403
