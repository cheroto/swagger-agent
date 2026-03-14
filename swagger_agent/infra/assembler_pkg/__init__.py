"""Assembler package — converts artifacts into an OpenAPI 3.0 spec.

Re-exports all public symbols for backward compatibility.
"""

__all__ = [
    "AssemblyResult", "assemble_spec",
    "_build_ref", "_build_operation", "_derive_security_scheme",
    "_parse_ref_hint", "_parse_union_ref_hint", "_sanitize_ref_hint",
    "_normalize_path", "_reconcile_path_params", "_replace_outside_braces",
    "_sanitize_path_template",
    "_break_ref_cycles", "_coerce_to_schema", "_deduplicate_operation_ids",
    "_extract_refs_from_schema", "_fix_ref_siblings", "_normalize_schema_case",
    "_sanitize_schemas", "_synthesize_polymorphism",
    "_fix_leaked_ref_hints",
    "inline_primitive_refs", "primitive_schema",
    "_fix_non_schema_properties",
]

from .assemble import (  # noqa: F401
    AssemblyResult,
    _build_ref,
    _derive_security_scheme,
    _build_operation,
    _parse_ref_hint,
    _parse_union_ref_hint,
    _sanitize_ref_hint,
    assemble_spec,
)
from .path_utils import (  # noqa: F401
    _normalize_path,
    _reconcile_path_params,
    _replace_outside_braces,
    _sanitize_path_template,
)
from .schema_fixups import (  # noqa: F401
    _break_ref_cycles,
    _coerce_to_schema,
    _deduplicate_operation_ids,
    _extract_refs_from_schema,
    _fix_ref_siblings,
    _normalize_schema_case,
    _fix_leaked_ref_hints,
    _sanitize_schemas,
    _synthesize_polymorphism,
    inline_primitive_refs,
    primitive_schema,
)

# Backward compatibility alias — tests import the old name
_fix_non_schema_properties = _sanitize_schemas
