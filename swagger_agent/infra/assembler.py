"""Assembler — converts artifacts into an OpenAPI 3.0 spec dict and YAML string."""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass

import yaml

from swagger_agent.models import DiscoveryManifest, EndpointDescriptor, Endpoint

logger = logging.getLogger("swagger_agent.assembler")


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
    """Combine base_path and endpoint path, normalizing to OpenAPI format.

    Handles:
    - Deduplication when endpoint_path already contains the base_path prefix
    - Conversion of :param and <param> to {param}
    - Validation of the resulting path template (nested braces, empty braces)
    """
    # Deduplicate: if endpoint path already starts with base_path, don't prepend
    stripped_base = base_path.rstrip("/")
    stripped_ep = endpoint_path.lstrip("/")
    if stripped_base and stripped_ep.startswith(stripped_base.lstrip("/")):
        full = f"/{stripped_ep}"
    else:
        full = f"{stripped_base}/{stripped_ep}"

    # Collapse double slashes (but keep the leading one)
    full = re.sub(r"//+", "/", full)
    if not full.startswith("/"):
        full = "/" + full

    # Strip route constraints from inside braces BEFORE :param conversion.
    # ASP.NET/Spring use {param:constraint} (e.g. {version:apiVersion}, {id:int}).
    # OpenAPI only wants {param} — the constraint is dropped.
    full = re.sub(r"\{(\w+):[^}]+\}", r"{\1}", full)

    # Convert framework-specific path param syntax to OpenAPI {param} style
    # :param (Express, Sinatra, Flask) and <param> (Flask, Django)
    full = re.sub(r":(\w+)", r"{\1}", full)
    full = re.sub(r"<(\w+)>", r"{\1}", full)

    # Validate and fix the path template
    full = _sanitize_path_template(full)
    return full


# Matches a well-formed path parameter: {word_chars}
_VALID_PARAM = re.compile(r"^\w+$")


def _sanitize_path_template(path: str) -> str:
    """Validate and fix path template brace syntax.

    Fixes:
    - Nested braces: {version{apiVersion}} → {apiVersion}
    - Empty braces: /path/{}/rest → /path/rest
    - Unclosed braces: /path/{param/rest → /path/{param}/rest
    - Unopened braces: /path/param}/rest → /path/param/rest
    """
    original = path

    # Fix nested braces: extract the innermost parameter name
    # e.g. {version{apiVersion}} → {apiVersion}
    while re.search(r"\{[^}]*\{", path):
        path = re.sub(r"\{[^{}]*\{(\w+)\}[^{}]*\}", r"{\1}", path)
        # Safety: break if no progress (prevents infinite loop on weird input)
        if path == original:
            break
        original = path

    # Remove empty braces
    if "{}" in path:
        logger.warning("Path template has empty braces, removing: %s", original)
        path = path.replace("{}", "")
        # Clean up doubled slashes from removal
        path = re.sub(r"//+", "/", path)

    # Fix unclosed braces: {param without closing }
    # Find { not followed by } before the next { or end of string
    unclosed = re.search(r"\{(\w+)(?=[/{]|$)(?!\})", path)
    if unclosed:
        logger.warning("Path template has unclosed brace, fixing: %s", original)
        path = re.sub(r"\{(\w+)(?=[/{]|$)(?!\})", r"{\1}", path)

    # Remove stray closing braces: any } not part of a {param} pair.
    # After all fixes above, well-formed params are {word_chars}.
    # Rebuild by removing } that aren't part of {...}.
    if path.count("{") != path.count("}"):
        logger.warning("Path template has mismatched braces, fixing: %s", path)
        # Remove } not preceded by a matching {
        fixed_parts: list[str] = []
        i = 0
        while i < len(path):
            if path[i] == "{":
                close = path.find("}", i)
                if close == -1:
                    # Unclosed { at end — already handled above, skip the {
                    fixed_parts.append(path[i + 1:])
                    break
                fixed_parts.append(path[i:close + 1])
                i = close + 1
            elif path[i] == "}":
                # Stray } — skip it
                i += 1
            else:
                fixed_parts.append(path[i])
                i += 1
        path = "".join(fixed_parts)
        path = re.sub(r"//+", "/", path)

    # Validate each {param} segment
    for match in re.finditer(r"\{([^}]*)\}", path):
        param_name = match.group(1)
        if not _VALID_PARAM.match(param_name):
            logger.warning(
                "Path parameter name is not a valid identifier: {%s} in %s",
                param_name, path,
            )

    # Remove trailing slash (except for root "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    if path != original:
        logger.info("Path template normalized: %s → %s", original, path)

    return path


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


_EMPTY_REF_PLACEHOLDER = {
    "type": "object",
    "description": "Empty schema reference",
    "x-unresolved": True,
}


def _build_ref(name: str) -> dict:
    is_array, inner = _parse_ref_hint(name)
    if not inner:
        return dict(_EMPTY_REF_PLACEHOLDER)
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


def _deduplicate_operation_ids(spec: dict) -> None:
    """Deduplicate operationIds by prefixing collisions with their first tag."""
    # Collect all (path, method) → operationId
    id_to_locations: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if isinstance(op, dict) and "operationId" in op:
                id_to_locations[op["operationId"]].append((path, method))

    # Only fix collisions
    for op_id, locations in id_to_locations.items():
        if len(locations) <= 1:
            continue
        for path, method in locations:
            op = spec["paths"][path][method]
            tags = op.get("tags", [])
            tag = tags[0] if tags else ""
            new_id = f"{tag}_{op_id}" if tag else f"{path.strip('/').replace('/', '_')}_{op_id}"
            op["operationId"] = new_id

        # Check for secondary collisions (unlikely but handle)
        seen: set[str] = set()
        for path, method in locations:
            op = spec["paths"][path][method]
            if op["operationId"] in seen:
                h = hashlib.md5(f"{path}:{method}".encode()).hexdigest()[:6]
                op["operationId"] = f"{op['operationId']}_{h}"
            seen.add(op["operationId"])


def _fix_ref_siblings(schema: object) -> object:
    """Wrap $ref + sibling keys with allOf (OpenAPI 3.0 requires it)."""
    if isinstance(schema, dict):
        if "$ref" in schema and len(schema) > 1:
            ref = schema.pop("$ref")
            schema["allOf"] = [{"$ref": ref}]
            # Recurse remaining values
            for k, v in schema.items():
                if k != "allOf":
                    schema[k] = _fix_ref_siblings(v)
        else:
            for k, v in schema.items():
                schema[k] = _fix_ref_siblings(v)
    elif isinstance(schema, list):
        for i, item in enumerate(schema):
            schema[i] = _fix_ref_siblings(item)
    return schema


def _break_ref_cycles(spec: dict) -> None:
    """Detect and break circular $ref chains in components/schemas."""
    schemas = spec.get("components", {}).get("schemas", {})
    if not schemas:
        return

    # Build adjacency: schema_name → set of (referenced_name, is_array_context)
    graph: dict[str, set[str]] = {}
    array_edges: set[tuple[str, str]] = set()
    for name, schema in schemas.items():
        refs = _extract_refs_from_schema(schema)
        graph[name] = refs
        # Detect which refs are inside array items
        _mark_array_edges(name, schema, array_edges)

    # DFS cycle detection — find all back edges
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in schemas}
    parent: dict[str, str | None] = {n: None for n in schemas}
    back_edges: list[tuple[str, str]] = []  # (from, to) where to is ancestor

    def dfs(u: str) -> None:
        color[u] = GRAY
        for v in graph.get(u, set()):
            if v not in color:
                continue
            if color[v] == WHITE:
                parent[v] = u
                dfs(v)
            elif color[v] == GRAY:
                back_edges.append((u, v))
        color[u] = BLACK

    for node in schemas:
        if color[node] == WHITE:
            dfs(node)

    # Break each back edge
    for src, tgt in back_edges:
        # Prefer cutting array-context edges
        if (src, tgt) in array_edges:
            _replace_ref_in_schema(schemas[src], tgt)
        elif (tgt, src) in array_edges:
            _replace_ref_in_schema(schemas[tgt], src)
            # Also update graph
        else:
            _replace_ref_in_schema(schemas[src], tgt)


def _mark_array_edges(schema_name: str, schema: object, array_edges: set[tuple[str, str]]) -> None:
    """Track which $ref edges are inside array items."""
    prefix = "#/components/schemas/"
    if isinstance(schema, dict):
        if schema.get("type") == "array" and "items" in schema:
            items = schema["items"]
            if isinstance(items, dict) and "$ref" in items:
                ref = items["$ref"]
                if isinstance(ref, str) and ref.startswith(prefix):
                    array_edges.add((schema_name, ref[len(prefix):]))
            _mark_array_edges(schema_name, items, array_edges)
        else:
            for v in schema.values():
                _mark_array_edges(schema_name, v, array_edges)
    elif isinstance(schema, list):
        for item in schema:
            _mark_array_edges(schema_name, item, array_edges)


def _replace_ref_in_schema(schema: dict, target_name: str) -> None:
    """Replace $ref to target_name with an inline circular-ref stub."""
    prefix = "#/components/schemas/"
    ref_value = f"{prefix}{target_name}"

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            # Check allOf wrappers too
            if "allOf" in obj and isinstance(obj["allOf"], list):
                for i, item in enumerate(obj["allOf"]):
                    if isinstance(item, dict) and item.get("$ref") == ref_value:
                        obj["allOf"][i] = {
                            "type": "object",
                            "description": f"Circular reference to {target_name}",
                            "x-circular-ref": ref_value,
                        }
                        return
            if obj.get("$ref") == ref_value:
                obj.pop("$ref")
                obj["type"] = "object"
                obj["description"] = f"Circular reference to {target_name}"
                obj["x-circular-ref"] = ref_value
                return
            if obj.get("type") == "array" and "items" in obj:
                items = obj["items"]
                if isinstance(items, dict) and items.get("$ref") == ref_value:
                    obj["items"] = {
                        "type": "object",
                        "description": f"Circular reference to {target_name}",
                        "x-circular-ref": ref_value,
                    }
                    return
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(schema)


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

            # Track referenced schema names (unwrap array wrappers, skip empty)
            if ep.request_body and ep.request_body.schema_ref:
                _, inner = _parse_ref_hint(ep.request_body.schema_ref.ref_hint)
                if inner:
                    referenced_schemas.add(inner)
            for resp in ep.responses:
                if resp.schema_ref:
                    _, inner = _parse_ref_hint(resp.schema_ref.ref_hint)
                    if inner:
                        referenced_schemas.add(inner)

    # Only emit schemas that are referenced by endpoints (directly or transitively via $ref)
    def _collect_transitive_refs(names: set[str], all_schemas: dict[str, dict]) -> set[str]:
        """Walk $ref chains to find all transitively referenced schemas."""
        result = {n for n in names if n}  # skip empty names
        frontier = set(result)
        while frontier:
            next_frontier: set[str] = set()
            for n in frontier:
                schema = all_schemas.get(n, {})
                for ref in _extract_refs_from_schema(schema):
                    if ref and ref not in result:
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

    # Post-processing passes (order matters)
    # 1. Fix $ref + sibling keys (must come before cycle detection)
    if spec.get("components", {}).get("schemas"):
        _fix_ref_siblings(spec["components"]["schemas"])
    # 2. Break circular $ref chains
    _break_ref_cycles(spec)
    # 3. Deduplicate operationIds
    _deduplicate_operation_ids(spec)

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
