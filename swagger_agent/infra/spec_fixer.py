"""Spec Fixer — deterministic post-processing that cleans up common OpenAPI spec issues.

Runs after assembly as a safety net to catch problems that the assembler's fixup
passes missed. All fixes are idempotent and structural — no LLM calls.

This module addresses the most common validation errors and warnings:
- nullable without type (OAS 3.0 requires type when nullable is used)
- identical paths (paths differing only by parameter names)
- missing 4xx responses on operations
- unused component schemas
- dangling $ref targets (refs pointing to non-existent schemas)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("swagger_agent.spec_fixer")

_REF_PREFIX = "#/components/schemas/"


@dataclass
class SpecFixResult:
    """Summary of what the fixer changed."""
    fixes_applied: list[str] = field(default_factory=list)
    total_fixes: int = 0


# ── Individual fixers ────────────────────────────────────────────────────


def _fix_nullable_without_type(spec: dict) -> list[str]:
    """Add type: object wherever nullable: true appears without a type field.

    OAS 3.0 requires 'type' when 'nullable' is used. The assembler's
    _fix_ref_siblings handles this for $ref siblings, but misses cases where
    nullable was set on allOf/oneOf/anyOf wrappers during _build_schema_for_ref.
    """
    fixes: list[str] = []

    def _walk(obj: object, path: str) -> None:
        if isinstance(obj, dict):
            if obj.get("nullable") and "type" not in obj:
                # Only add type if there's no $ref (which would need allOf wrapping,
                # but that should already be handled by _fix_ref_siblings)
                if "$ref" not in obj:
                    obj["type"] = "object"
                    fixes.append(f"Added type:object to nullable node at {path}")
            for k, v in obj.items():
                _walk(v, f"{path}/{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _walk(v, f"{path}[{i}]")

    _walk(spec, "#")
    return fixes


def _fix_identical_paths(spec: dict) -> list[str]:
    """Merge paths that differ only by parameter names.

    OAS 3.0 treats /api/{slug} and /api/{id} as identical. When both exist,
    merge all methods from the duplicate into the canonical path and remove
    the duplicate.
    """
    fixes: list[str] = []
    paths = spec.get("paths", {})
    if not paths:
        return fixes

    # Group paths by their normalized form (all params replaced with {_})
    norm_to_paths: dict[str, list[str]] = {}
    for path_key in list(paths.keys()):
        norm = re.sub(r"\{[^}]+\}", "{_}", path_key)
        norm_to_paths.setdefault(norm, []).append(path_key)

    for _norm, path_keys in norm_to_paths.items():
        if len(path_keys) <= 1:
            continue

        # Keep the first path as canonical, merge others into it
        canonical = path_keys[0]
        for dup in path_keys[1:]:
            dup_methods = paths[dup]
            for method, op in dup_methods.items():
                if not isinstance(op, dict):
                    continue
                if method not in paths[canonical]:
                    paths[canonical][method] = op
                    fixes.append(
                        f"Merged {method.upper()} from '{dup}' into '{canonical}'"
                    )
                # If method already exists in canonical, skip (keep canonical's version)

            del paths[dup]
            fixes.append(f"Removed identical path '{dup}' (merged into '{canonical}')")

    return fixes


def _add_missing_4xx_responses(spec: dict) -> list[str]:
    """Add a default 4xx response to operations that have none.

    Redocly warns on operation-4xx-response when no 4xx status code exists.
    We add a generic 422 for all operations, and 401/403 for protected ones.
    """
    fixes: list[str] = []
    paths = spec.get("paths", {})

    for path, methods in paths.items():
        for method, op in methods.items():
            if not isinstance(op, dict):
                continue
            responses = op.get("responses", {})
            if not responses:
                continue

            op_id = op.get("operationId", f"{method.upper()} {path}")

            # Check if any 4xx response exists
            has_4xx = any(
                str(code).startswith("4") for code in responses
            )

            if not has_4xx:
                # Add generic 422 Validation Error
                responses["422"] = {"description": "Validation Error"}
                fixes.append(f"Added 422 response to {op_id}")

                # Also add 401/403 for protected endpoints
                security = op.get("security")
                if security and len(security) > 0:
                    if "401" not in responses:
                        responses["401"] = {"description": "Unauthorized"}
                        fixes.append(f"Added 401 response to {op_id}")
                    if "403" not in responses:
                        responses["403"] = {"description": "Forbidden"}
                        fixes.append(f"Added 403 response to {op_id}")

                op["responses"] = responses

    return fixes


def _remove_unused_schemas(spec: dict) -> list[str]:
    """Remove schemas from components/schemas that are never referenced.

    Walks the entire spec to collect all $ref targets, then removes any
    schema that is not referenced by paths or other schemas.
    """
    fixes: list[str] = []
    schemas = spec.get("components", {}).get("schemas", {})
    if not schemas:
        return fixes

    # Collect all $ref targets from the entire spec
    referenced: set[str] = set()

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            ref = obj.get("$ref", "")
            if isinstance(ref, str) and ref.startswith(_REF_PREFIX):
                referenced.add(ref[len(_REF_PREFIX):])
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    # Walk paths
    _walk(spec.get("paths", {}))
    # Walk schemas (schemas can reference other schemas)
    _walk(schemas)

    # Remove unreferenced schemas
    to_remove = [name for name in schemas if name not in referenced]
    for name in to_remove:
        del schemas[name]
        fixes.append(f"Removed unused schema '{name}'")

    return fixes


def _fix_dangling_refs(spec: dict) -> list[str]:
    """Fix $ref targets that point to non-existent schemas.

    When a $ref points to a schema that doesn't exist in components/schemas,
    replace it with an inline object placeholder.
    """
    fixes: list[str] = []
    schemas = spec.get("components", {}).get("schemas", {})

    def _walk(obj: object, path: str) -> None:
        if isinstance(obj, dict):
            ref = obj.get("$ref", "")
            if isinstance(ref, str) and ref.startswith(_REF_PREFIX):
                target = ref[len(_REF_PREFIX):]
                if target not in schemas:
                    del obj["$ref"]
                    obj["type"] = "object"
                    obj["description"] = f"Unresolved reference to {target}"
                    obj["x-unresolved"] = True
                    fixes.append(f"Replaced dangling $ref to '{target}' at {path}")
                    return
            for k, v in obj.items():
                _walk(v, f"{path}/{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _walk(v, f"{path}[{i}]")

    _walk(spec.get("paths", {}), "#/paths")
    return fixes


def _fix_empty_schema_components(spec: dict) -> list[str]:
    """Clean up empty components after other fixers remove schemas."""
    fixes: list[str] = []
    components = spec.get("components", {})
    schemas = components.get("schemas", {})

    if not schemas and "schemas" in components:
        del components["schemas"]
        fixes.append("Removed empty components/schemas")

    if not components and "components" in spec:
        del spec["components"]
        fixes.append("Removed empty components")

    return fixes


# ── Main entry point ─────────────────────────────────────────────────────


def fix_spec(spec: dict) -> SpecFixResult:
    """Apply all deterministic fixes to the spec dict (mutates in place).

    Returns a SpecFixResult with details of what was changed.
    Order matters: identical paths first (merging), then content fixes,
    then unused schema removal (after refs are resolved), then cleanup.
    """
    result = SpecFixResult()

    fixers = [
        _fix_identical_paths,
        _fix_nullable_without_type,
        _add_missing_4xx_responses,
        _fix_dangling_refs,
        _remove_unused_schemas,
        _fix_empty_schema_components,
    ]

    for fixer in fixers:
        fixes = fixer(spec)
        if fixes:
            result.fixes_applied.extend(fixes)
            for fix in fixes:
                logger.info("Spec fix: %s", fix)

    result.total_fixes = len(result.fixes_applied)
    return result
