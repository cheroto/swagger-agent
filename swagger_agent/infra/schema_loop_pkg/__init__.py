"""Schema loop package — resolves ref_hints and extracts schemas iteratively.

Re-exports all public symbols for backward compatibility.
"""

__all__ = [
    "_decompose_type_hint", "_split_generic_args",
    "_BUILTIN_TYPES", "_PASSTHROUGH_WRAPPERS",
    "collect_ref_hints_from_descriptor", "run_schema_loop",
    "print_schema_summary", "main",
]

from .type_hints import (  # noqa: F401
    _decompose_type_hint,
    _split_generic_args,
    _BUILTIN_TYPES,
    _PASSTHROUGH_WRAPPERS,
)
from .loop import (  # noqa: F401
    collect_ref_hints_from_descriptor,
    run_schema_loop,
    print_schema_summary,
    main,
)
