"""Core spec assembly — builds OpenAPI 3.0 from pipeline artifacts."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import yaml

from swagger_agent.models import DiscoveryManifest, EndpointDescriptor, Endpoint

from .path_utils import _normalize_path, _reconcile_path_params
from .schema_fixups import (
    _break_ref_cycles,
    _deduplicate_operation_ids,
    _extract_refs_from_schema,
    _fix_leaked_ref_hints,
    _fix_ref_siblings,
    _normalize_schema_case,
    _sanitize_schemas,
    _synthesize_polymorphism,
    inline_primitive_refs,
    primitive_schema,
)

logger = logging.getLogger("swagger_agent.assembler")


@dataclass
class AssemblyResult:
    spec: dict
    yaml_str: str


# ── Ref hint parsing ─────────────────────────────────────────────────────

_ARRAY_PATTERN = re.compile(
    r"^(?:"
    r"\[\](.+)"           # Go:      []Type
    r"|(.+)\[\]"          # TS/Java: Type[]
    r"|\w+\[(.+)\]"      # Generic[T]: List[T], Array[T], Sequence[T], Set[T], etc.
    r"|\w+<(.+)>"        # Generic<T>: List<T>, Vec<T>, IEnumerable<T>, etc.
    r")$"
)

_REF_PREFIX = "#/components/schemas/"

_UNION_RE = re.compile(r"^Union\[(.+)\]$")

_EMPTY_REF_PLACEHOLDER = {
    "type": "object",
    "description": "Empty schema reference",
    "x-unresolved": True,
}


def _sanitize_ref_hint(name: str) -> str:
    """Strip stale $ref prefixes that LLMs sometimes include in ref_hint values."""
    while name.startswith(_REF_PREFIX):
        name = name[len(_REF_PREFIX):]
    return name.strip()


def _parse_ref_hint(name: str) -> tuple[bool, str]:
    """Parse a ref_hint, detecting array wrappers.

    Returns (is_array, inner_type_name).
    """
    name = _sanitize_ref_hint(name)
    m = re.match(r"^Optional\[(.+)\]$", name)
    if m:
        name = m.group(1).strip()
    m = _ARRAY_PATTERN.match(name)
    if m:
        inner = next(g for g in m.groups() if g is not None)
        return True, inner.strip()
    return False, name


def _parse_union_ref_hint(name: str) -> list[str] | None:
    """Detect Union[A, B, C] patterns and return individual type names.

    Returns None if not a Union, otherwise a list of inner type names.
    """
    name = _sanitize_ref_hint(name)
    m = _UNION_RE.match(name)
    if not m:
        return None
    inner = m.group(1)
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in inner:
        if ch in ("[", "<"):
            depth += 1
            current.append(ch)
        elif ch in ("]", ">"):
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    remainder = "".join(current).strip()
    if remainder:
        parts.append(remainder)
    return [p for p in parts if p]


def _build_schema_for_ref(ref_hint_obj, ref_rewrite: dict[str, str] | None = None) -> dict:
    """Build the schema dict for a RefHint, applying name rewrites if any."""
    name = ref_hint_obj.ref_hint
    if ref_rewrite:
        name = ref_rewrite.get(name, name)
    return _build_ref(name)


def _build_ref(name: str) -> dict:
    """Build an OpenAPI $ref (or oneOf for unions, array for collections)."""
    union_parts = _parse_union_ref_hint(name)
    if union_parts:
        return {
            "oneOf": [_build_ref(part) for part in union_parts],
        }
    is_array, inner = _parse_ref_hint(name)
    if not inner:
        return dict(_EMPTY_REF_PLACEHOLDER)
    if is_array:
        return {
            "type": "array",
            "items": {"$ref": f"#/components/schemas/{inner}"},
        }
    return {"$ref": f"#/components/schemas/{inner}"}


# ── Operation / security helpers ─────────────────────────────────────────

def _derive_security_scheme(name: str) -> dict:
    """Heuristic: map a security scheme name to an OpenAPI securitySchemes entry."""
    low = name.lower()
    if any(k in low for k in ("bearer", "jwt", "token")):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    if any(k in low for k in ("apikey", "api_key", "api-key")):
        return {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    if "basic" in low:
        return {"type": "http", "scheme": "basic"}
    return {"type": "http", "scheme": "bearer"}


def _build_operation(
    ep: Endpoint,
    ref_rewrite: dict[str, str] | None = None,
) -> dict:
    """Build an OpenAPI operation object from an Endpoint model."""
    op: dict = {"operationId": ep.operation_id}

    if ep.tags:
        op["tags"] = ep.tags

    if ep.parameters:
        seen: dict[tuple[str, str], dict] = {}
        for p in ep.parameters:
            dumped = p.model_dump(by_alias=True, exclude_none=True)
            key = (dumped["name"], dumped["in"])
            seen[key] = dumped
        op["parameters"] = list(seen.values())

    if ep.request_body:
        rb = ep.request_body
        if rb.schema_ref:
            schema = _build_schema_for_ref(rb.schema_ref, ref_rewrite)
        else:
            schema = {"type": "object"}
        op["requestBody"] = {
            "content": {rb.content_type: {"schema": schema}},
        }

    if ep.responses:
        responses: dict = {}
        for resp in ep.responses:
            entry: dict = {"description": resp.description or f"Response {resp.status_code}"}
            if resp.schema_ref:
                entry["content"] = {
                    "application/json": {
                        "schema": _build_schema_for_ref(resp.schema_ref, ref_rewrite),
                    }
                }
            responses[resp.status_code] = entry
        op["responses"] = responses
    else:
        op["responses"] = {"200": {"description": "OK"}}

    if len(ep.security) == 0:
        op["security"] = []
    else:
        op["security"] = [{scheme: []} for scheme in ep.security]

    return op


# ── Main assembly ────────────────────────────────────────────────────────

def assemble_spec(
    manifest: DiscoveryManifest,
    descriptors: list[EndpointDescriptor],
    schemas: dict[str, dict],
    *,
    inheritance_map: dict | None = None,
    name_mapping: dict[tuple[str, str], str] | None = None,
) -> AssemblyResult:
    """Assemble a full OpenAPI 3.0 spec from pipeline artifacts."""
    spec: dict = {
        "openapi": "3.0.3",
        "info": {"title": "API Specification", "version": "1.0.0"},
        "servers": [{"url": s} for s in manifest.servers] or [{"url": "http://localhost:8080"}],
        "paths": {},
        "components": {"schemas": {}, "securitySchemes": {}},
    }

    # Security schemes
    all_scheme_names: set[str] = set()
    for desc in descriptors:
        for ep in desc.endpoints:
            if ep.security:
                all_scheme_names.update(ep.security)

    for name in sorted(all_scheme_names):
        spec["components"]["securitySchemes"][name] = _derive_security_scheme(name)

    if not spec["components"]["securitySchemes"]:
        del spec["components"]["securitySchemes"]

    # Paths and endpoints
    referenced_schemas: set[str] = set()

    for desc in descriptors:
        # Build per-descriptor ref rewrite map from name_mapping
        ref_rewrite: dict[str, str] | None = None
        if name_mapping:
            rw = {}
            for (orig_name, src_file), qualified in name_mapping.items():
                if src_file == desc.source_file and orig_name != qualified:
                    rw[orig_name] = qualified
            ref_rewrite = rw or None

        for ep in desc.endpoints:
            path_key = _normalize_path(manifest.base_path, ep.path)
            method = ep.method.lower()

            _reconcile_path_params(path_key, ep)

            if path_key not in spec["paths"]:
                spec["paths"][path_key] = {}

            spec["paths"][path_key][method] = _build_operation(ep, ref_rewrite)

            # Track referenced schema names (using rewritten names)
            for ref_source in (
                [ep.request_body.schema_ref] if ep.request_body and ep.request_body.schema_ref else []
            ) + [resp.schema_ref for resp in ep.responses if resp.schema_ref]:
                hint_name = ref_source.ref_hint
                if ref_rewrite:
                    hint_name = ref_rewrite.get(hint_name, hint_name)
                union_parts = _parse_union_ref_hint(hint_name)
                if union_parts:
                    for part in union_parts:
                        _, inner = _parse_ref_hint(part)
                        if inner:
                            referenced_schemas.add(inner)
                else:
                    _, inner = _parse_ref_hint(hint_name)
                    if inner:
                        referenced_schemas.add(inner)

    # Resolve transitive refs and populate components/schemas
    schemas_lower: dict[str, str] = {}
    for key in schemas:
        schemas_lower.setdefault(key.lower(), key)

    all_needed = _collect_transitive_refs(referenced_schemas, schemas, schemas_lower)

    for name in all_needed:
        # Skip primitive types — they'll be inlined by inline_primitive_refs
        if primitive_schema(name) is not None:
            continue
        if name in schemas:
            spec["components"]["schemas"][name] = schemas[name]
        else:
            actual = schemas_lower.get(name.lower())
            if actual:
                logger.info("Case-insensitive schema match: %s → %s", name, actual)
                spec["components"]["schemas"][name] = schemas[actual]
            else:
                spec["components"]["schemas"][name] = {
                    "type": "object",
                    "description": (
                        "Schema could not be resolved from source code. "
                        "Referenced by endpoint but not found in extracted schemas."
                    ),
                    "x-unresolved": True,
                }

    # Post-processing
    # Fix leaked RefHint dicts in parameter schemas (convert to $ref)
    _fix_leaked_ref_hints(spec)
    # Inline primitive $refs across the entire spec (paths + schemas)
    inline_primitive_refs(spec)
    schemas_dict = spec.get("components", {}).get("schemas")
    if schemas_dict:
        _sanitize_schemas(schemas_dict)
        _fix_ref_siblings(schemas_dict)
    _break_ref_cycles(spec)
    _normalize_schema_case(spec)
    _deduplicate_operation_ids(spec)
    if inheritance_map:
        _synthesize_polymorphism(spec, inheritance_map)

    if not spec["components"]["schemas"]:
        del spec["components"]["schemas"]
    if not spec.get("components"):
        del spec["components"]

    yaml_str = yaml.dump(
        spec,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )

    return AssemblyResult(spec=spec, yaml_str=yaml_str)


def _collect_transitive_refs(
    names: set[str],
    all_schemas: dict[str, dict],
    schemas_lower: dict[str, str],
) -> set[str]:
    """Walk $ref chains to find all transitively referenced schemas."""
    result = {n for n in names if n}
    frontier = set(result)
    while frontier:
        next_frontier: set[str] = set()
        for n in frontier:
            schema = all_schemas.get(n)
            if schema is None:
                actual = schemas_lower.get(n.lower())
                if actual:
                    schema = all_schemas.get(actual)
            if schema is None:
                schema = {}
            for ref in _extract_refs_from_schema(schema):
                if ref and ref not in result:
                    result.add(ref)
                    next_frontier.add(ref)
        frontier = next_frontier
    return result
