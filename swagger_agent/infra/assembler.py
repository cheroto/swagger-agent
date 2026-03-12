"""Assembler — converts artifacts into an OpenAPI 3.0 spec dict and YAML string."""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

from swagger_agent.models import DiscoveryManifest, EndpointDescriptor, Endpoint


@dataclass
class AssemblyResult:
    spec: dict
    yaml_str: str


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


def _normalize_path(base_path: str, endpoint_path: str) -> str:
    """Combine base_path and endpoint path, normalizing to OpenAPI format."""
    full = f"{base_path.rstrip('/')}/{endpoint_path.lstrip('/')}"
    # Collapse double slashes (but keep the leading one)
    full = re.sub(r"//+", "/", full)
    if not full.startswith("/"):
        full = "/" + full
    # Convert framework-specific path param syntax to OpenAPI {param} style
    # :param (Express, Sinatra, Flask) and <param> (Flask, Django)
    full = re.sub(r":(\w+)", r"{\1}", full)
    full = re.sub(r"<(\w+)>", r"{\1}", full)
    return full


# Language-agnostic array/collection wrapper detection.
# Matches any Generic<T>, Generic[T], []T, or T[] pattern — no need to
# enumerate specific wrapper names per language.
_ARRAY_PATTERN = re.compile(
    r"^(?:"
    r"\[\](.+)"           # Go:      []Type
    r"|(.+)\[\]"          # TS/Java: Type[]
    r"|\w+\[(.+)\]"      # Generic[T]: List[T], Array[T], Sequence[T], Set[T], etc.
    r"|\w+<(.+)>"        # Generic<T>: List<T>, Vec<T>, IEnumerable<T>, etc.
    r")$"
)


_REF_PREFIX = "#/components/schemas/"


def _sanitize_ref_hint(name: str) -> str:
    """Strip stale $ref prefixes that LLMs sometimes include in ref_hint values.

    Handles both single and repeated prefixes (e.g.
    "#/components/schemas/User" → "User",
    "#/components/schemas/#/components/schemas/User" → "User").
    """
    while name.startswith(_REF_PREFIX):
        name = name[len(_REF_PREFIX):]
    return name.strip()


def _parse_ref_hint(name: str) -> tuple[bool, str]:
    """Parse a ref_hint, detecting array wrappers.

    Sanitizes stale $ref prefixes before parsing.
    Returns (is_array, inner_type_name).
    """
    name = _sanitize_ref_hint(name)
    m = _ARRAY_PATTERN.match(name)
    if m:
        inner = next(g for g in m.groups() if g is not None)
        return True, inner.strip()
    return False, name


def _extract_refs_from_schema(schema: dict) -> set[str]:
    """Extract all schema names referenced via $ref in a schema dict."""
    refs: set[str] = set()
    prefix = "#/components/schemas/"

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            if "$ref" in obj and isinstance(obj["$ref"], str) and obj["$ref"].startswith(prefix):
                refs.add(obj["$ref"][len(prefix):])
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(schema)
    return refs


def _build_ref(name: str) -> dict:
    is_array, inner = _parse_ref_hint(name)
    if is_array:
        return {
            "type": "array",
            "items": {"$ref": f"#/components/schemas/{inner}"},
        }
    return {"$ref": f"#/components/schemas/{inner}"}


def _build_operation(ep: Endpoint) -> dict:
    """Build an OpenAPI operation object from an Endpoint model."""
    op: dict = {"operationId": ep.operation_id}

    if ep.tags:
        op["tags"] = ep.tags

    # Parameters (deduplicate by name+in, keep last occurrence)
    if ep.parameters:
        seen: dict[tuple[str, str], dict] = {}
        for p in ep.parameters:
            dumped = p.model_dump(by_alias=True, exclude_none=True)
            key = (dumped["name"], dumped["in"])
            seen[key] = dumped
        op["parameters"] = list(seen.values())

    # Request body
    if ep.request_body:
        rb = ep.request_body
        if rb.schema_ref:
            schema = _build_ref(rb.schema_ref.ref_hint)
        else:
            schema = {"type": "object"}
        op["requestBody"] = {
            "content": {rb.content_type: {"schema": schema}},
        }

    # Responses
    if ep.responses:
        responses: dict = {}
        for resp in ep.responses:
            entry: dict = {"description": resp.description or f"Response {resp.status_code}"}
            if resp.schema_ref:
                entry["content"] = {
                    "application/json": {"schema": _build_ref(resp.schema_ref.ref_hint)}
                }
            responses[resp.status_code] = entry
        op["responses"] = responses
    else:
        op["responses"] = {"200": {"description": "OK"}}

    # Security
    if ep.security is not None:
        if len(ep.security) == 0:
            op["security"] = []  # explicitly public
        else:
            op["security"] = [{scheme: []} for scheme in ep.security]

    return op


def assemble_spec(
    manifest: DiscoveryManifest,
    descriptors: list[EndpointDescriptor],
    schemas: dict[str, dict],
) -> AssemblyResult:
    """Assemble a full OpenAPI 3.0 spec from pipeline artifacts.

    Args:
        manifest: Scout discovery manifest.
        descriptors: Route extractor endpoint descriptors.
        schemas: Resolved schemas from the schema loop.

    Returns:
        AssemblyResult with the spec dict and YAML string.
    """
    spec: dict = {
        "openapi": "3.0.3",
        "info": {"title": "API Specification", "version": "1.0.0"},
        "servers": [{"url": s} for s in manifest.servers] or [{"url": "http://localhost:8080"}],
        "paths": {},
        "components": {"schemas": {}, "securitySchemes": {}},
    }

    # Collect all security scheme names from endpoints
    all_scheme_names: set[str] = set()
    for desc in descriptors:
        for ep in desc.endpoints:
            if ep.security:
                all_scheme_names.update(ep.security)

    # Derive securitySchemes
    for name in sorted(all_scheme_names):
        spec["components"]["securitySchemes"][name] = _derive_security_scheme(name)

    # Remove empty securitySchemes
    if not spec["components"]["securitySchemes"]:
        del spec["components"]["securitySchemes"]

    # Map endpoints to paths
    # Collect all ref_hint names referenced by endpoints
    referenced_schemas: set[str] = set()

    for desc in descriptors:
        for ep in desc.endpoints:
            path_key = _normalize_path(manifest.base_path, ep.path)
            method = ep.method.lower()

            if path_key not in spec["paths"]:
                spec["paths"][path_key] = {}

            spec["paths"][path_key][method] = _build_operation(ep)

            # Track referenced schema names (unwrap array wrappers)
            if ep.request_body and ep.request_body.schema_ref:
                _, inner = _parse_ref_hint(ep.request_body.schema_ref.ref_hint)
                referenced_schemas.add(inner)
            for resp in ep.responses:
                if resp.schema_ref:
                    _, inner = _parse_ref_hint(resp.schema_ref.ref_hint)
                    referenced_schemas.add(inner)

    # Only emit schemas that are referenced by endpoints (directly or transitively via $ref)
    def _collect_transitive_refs(names: set[str], all_schemas: dict[str, dict]) -> set[str]:
        """Walk $ref chains to find all transitively referenced schemas."""
        result = set(names)
        frontier = set(names)
        while frontier:
            next_frontier: set[str] = set()
            for n in frontier:
                schema = all_schemas.get(n, {})
                for ref in _extract_refs_from_schema(schema):
                    if ref not in result:
                        result.add(ref)
                        next_frontier.add(ref)
            frontier = next_frontier
        return result

    all_needed = _collect_transitive_refs(referenced_schemas, schemas)

    for name in all_needed:
        if name in schemas:
            spec["components"]["schemas"][name] = schemas[name]
        else:
            spec["components"]["schemas"][name] = {
                "type": "object",
                "description": (
                    "Schema could not be resolved from source code. "
                    "Referenced by endpoint but not found in extracted schemas."
                ),
                "x-unresolved": True,
            }

    # Remove empty schemas
    if not spec["components"]["schemas"]:
        del spec["components"]["schemas"]

    # Remove empty components
    if not spec.get("components"):
        del spec["components"]

    yaml_str = yaml.dump(
        spec,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )

    return AssemblyResult(spec=spec, yaml_str=yaml_str)
