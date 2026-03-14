"""Assembler — backward-compatible re-export shim.

The assembler has been split into:
  - assembler_pkg/path_utils.py    — path normalization, param reconciliation
  - assembler_pkg/schema_fixups.py — schema post-processing passes
  - assembler_pkg/assemble.py      — core spec assembly, ref parsing
"""

from swagger_agent.infra.assembler_pkg import *  # noqa: F401, F403
