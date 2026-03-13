"""Assembler — converts artifacts into an OpenAPI 3.0 spec dict and YAML string."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass

import yaml

from swagger_agent.models import DiscoveryManifest, EndpointDescriptor, Endpoint, Parameter

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


def _replace_outside_braces(path: str, pattern: str, repl: str) -> str:
    """Apply a regex substitution only to text outside of {...} segments."""
    result: list[str] = []
    i = 0
    while i < len(path):
        if path[i] == "{":
            close = path.find("}", i)
            if close == -1:
                # No closing brace — treat rest as outside
                result.append(re.sub(pattern, repl, path[i:]))
                break
            # Keep the braced segment verbatim
            result.append(path[i:close + 1])
            i = close + 1
        else:
            # Find the next opening brace or end of string
            next_open = path.find("{", i)
            if next_open == -1:
                result.append(re.sub(pattern, repl, path[i:]))
                break
            result.append(re.sub(pattern, repl, path[i:next_open]))
            i = next_open
    return "".join(result)


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

    # Convert framework-specific path param syntax to OpenAPI {param} style.
    # ONLY match :param and <param> OUTSIDE of braces — colons inside {param:constraint}
    # are route constraints (ASP.NET, Spring), not path parameter markers.
    # The LLM is responsible for resolving constraints to clean {param} paths.
    full = _replace_outside_braces(full, r":(\w+)", r"{\1}")
    full = re.sub(r"<(\w+)>", r"{\1}", full)

    # Validate and fix the path template
    full = _sanitize_path_template(full)
    return full


def _reconcile_path_params(path_key: str, ep: Endpoint) -> None:
    """Ensure parameter objects match the names in the path template.

    After _normalize_path may have renamed parameters (e.g. stripping
    constraints: {version:apiVersion} → {version}), this function
    renames any path parameter objects whose name matches a constraint
    that was stripped, so path template and parameter objects stay consistent.

    Also removes orphaned path parameters (params declared as "in: path"
    but whose name doesn't appear in the path template and can't be
    reconciled).

    This is agnostic — it compares the set of {names} in the path against
    the set of path parameter names in the endpoint, and fixes mismatches.
    """
    # Extract parameter names from the path template
    path_params = set(re.findall(r"\{(\w+)\}", path_key))

    if not path_params:
        return

    if not ep.parameters:
        ep.parameters = []

    # Collect current path parameter names from the endpoint
    ep_path_params = {
        p.name for p in ep.parameters if p.in_ == "path"
    }

    # Find mismatches: names in endpoint params that aren't in path template
    extra_in_ep = ep_path_params - path_params
    missing_in_ep = path_params - ep_path_params

    if extra_in_ep and missing_in_ep:
        # Try to match extras to missing by position in the path.
        if len(extra_in_ep) == 1 and len(missing_in_ep) == 1:
            old_name = extra_in_ep.pop()
            new_name = missing_in_ep.pop()
            for p in ep.parameters:
                if p.in_ == "path" and p.name == old_name:
                    logger.info(
                        "Reconciling path parameter: %s → %s (path template: %s)",
                        old_name, new_name, path_key,
                    )
                    p.name = new_name
                    break
        else:
            # Multiple mismatches: match by segment position in the path.
            norm_params = re.findall(r"\{(\w+)\}", path_key)
            missing_ordered = [p for p in norm_params if p in missing_in_ep]
            ep_extra_ordered = [
                p.name for p in ep.parameters
                if p.in_ == "path" and p.name in extra_in_ep
            ]

            # Pair up as many as possible by position
            pairs = min(len(missing_ordered), len(ep_extra_ordered))
            if pairs > 0:
                rename_map = dict(zip(ep_extra_ordered[:pairs], missing_ordered[:pairs]))
                for p in ep.parameters:
                    if p.in_ == "path" and p.name in rename_map:
                        logger.info(
                            "Reconciling path parameter: %s → %s (path template: %s)",
                            p.name, rename_map[p.name], path_key,
                        )
                        p.name = rename_map[p.name]

        # Recalculate after reconciliation
        ep_path_params = {p.name for p in ep.parameters if p.in_ == "path"}
        extra_in_ep = ep_path_params - path_params

    # Remove orphaned path parameters — params declared as "in: path" but
    # not present in the path template after reconciliation. These cause
    # OpenAPI validation errors ("parameter not found in path template").
    if extra_in_ep:
        for orphan in extra_in_ep:
            logger.info(
                "Removing orphaned path parameter '%s' not in path template: %s",
                orphan, path_key,
            )
        ep.parameters = [
            p for p in ep.parameters
            if not (p.in_ == "path" and p.name in extra_in_ep)
        ]

    # Add missing path parameters — params in the path template that have
    # no corresponding parameter object. These cause "path template expression
    # not matched with Parameter Object" validation errors.
    ep_path_params_final = {p.name for p in ep.parameters if p.in_ == "path"}
    still_missing = path_params - ep_path_params_final
    if still_missing:
        for name in still_missing:
            logger.info(
                "Adding missing path parameter '%s' from path template: %s",
                name, path_key,
            )
            ep.parameters.append(
                Parameter(name=name, in_="path", required=True,
                          schema_={"type": "string"})
            )


def _sanitize_path_template(path: str) -> str:
    """Validate and fix path template issues.

    Strips unresolved route constraints ({param:constraint} → {param})
    and removes trailing slashes.
    """
    original = path

    # Strip unresolved route constraints: {param:constraint} → {param}
    # Fallback — the LLM should resolve these, but if it doesn't,
    # infrastructure strips the constraint to produce valid OpenAPI.
    constraint_pattern = re.compile(r"\{(\w+):[^}]+\}")
    if constraint_pattern.search(path):
        logger.warning(
            "Path has unresolved route constraints (LLM should have resolved these): %s",
            path,
        )
        path = constraint_pattern.sub(r"{\1}", path)

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

    # Security — always emit (security is now a required list, never None)
    if len(ep.security) == 0:
        op["security"] = []  # explicitly public
    else:
        op["security"] = [{scheme: []} for scheme in ep.security]

    return op


def _scrub_string_schemas(obj: object) -> object:
    """Recursively fix string values that should be dicts (JSON objects).

    LLMs sometimes produce schema property values as JSON strings instead of
    actual dicts, e.g. '{"$ref": "#/components/schemas/Foo"}' instead of
    {"$ref": "..."}. This causes "Properties members must be schemas" errors.

    Also handles bare $ref strings used as property values (not as $ref targets),
    e.g. a property value of '#/components/schemas/Foo' gets converted to
    {"$ref": "..."}.
    """
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str):
                # Skip keys that legitimately have string values
                # ($ref targets, type, format, description, etc.)
                if k in ("$ref", "type", "format", "description", "pattern",
                         "title", "default", "example", "x-circular-ref"):
                    continue
                # Try to parse JSON strings that look like objects or arrays
                stripped = v.strip()
                if (stripped.startswith("{") and stripped.endswith("}")) or \
                   (stripped.startswith("[") and stripped.endswith("]")):
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, (dict, list)):
                            obj[k] = _scrub_string_schemas(parsed)
                            continue
                    except (json.JSONDecodeError, ValueError):
                        pass
                # Bare $ref string as a property value (not a $ref key)
                elif stripped.startswith("#/components/schemas/"):
                    obj[k] = {"$ref": stripped}
                    continue
            obj[k] = _scrub_string_schemas(v)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            obj[i] = _scrub_string_schemas(item)
    return obj


_JSON_SCHEMA_TYPES = frozenset({
    "string", "integer", "number", "boolean", "array", "object",
})


def _coerce_to_schema(value: object) -> dict:
    """Convert a non-dict value into a valid JSON Schema object.

    Handles common LLM mistakes: bare type names, $ref strings, null, etc.
    """
    if isinstance(value, str):
        stripped = value.strip()
        low = stripped.lower()
        # Bare JSON Schema type name: "string" → {"type": "string"}
        if low in _JSON_SCHEMA_TYPES:
            return {"type": low}
        # Bare $ref path
        if stripped.startswith("#/components/schemas/"):
            return {"$ref": stripped}
        # JSON string that looks like an object or array
        if (stripped.startswith("{") and stripped.endswith("}")) or \
           (stripped.startswith("[") and stripped.endswith("]")):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
        # PascalCase type name → assume $ref
        if stripped and stripped[0].isupper() and stripped.isidentifier():
            return {"$ref": f"#/components/schemas/{stripped}"}
        # Fallback
        return {"type": "string"}
    if value is None:
        return {"type": "string", "nullable": True}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    if isinstance(value, list):
        return {"type": "array"}
    return {"type": "object"}


def _fix_non_schema_properties(obj: object) -> None:
    """Ensure all values inside 'properties' dicts are valid schema objects.

    LLMs sometimes produce bare strings as property values instead of schema
    objects, causing 'properties members must be schemas' validation errors.
    """
    if isinstance(obj, dict):
        if "properties" in obj and isinstance(obj["properties"], dict):
            props = obj["properties"]
            for key, val in list(props.items()):
                if not isinstance(val, dict):
                    coerced = _coerce_to_schema(val)
                    logger.info(
                        "Coerced non-schema property '%s' value %r → %s",
                        key, val, coerced,
                    )
                    props[key] = coerced
        for v in obj.values():
            _fix_non_schema_properties(v)
    elif isinstance(obj, list):
        for item in obj:
            _fix_non_schema_properties(item)


def _strip_empty_required(obj: object) -> None:
    """Remove 'required': [] from schemas (OpenAPI 3.0 requires non-empty if present)."""
    if isinstance(obj, dict):
        if "required" in obj and isinstance(obj["required"], list) and len(obj["required"]) == 0:
            del obj["required"]
        for v in obj.values():
            _strip_empty_required(v)
    elif isinstance(obj, list):
        for item in obj:
            _strip_empty_required(item)


def _normalize_schema_case(spec: dict) -> None:
    """Fix case mismatches between $ref targets and schema keys.

    When an LLM extracts a schema under a different case than the ref_hint
    used in endpoint descriptors, $refs break. This pass collects all $ref
    targets, finds case-insensitive matches in the schema keys, and renames
    the keys to match the $ref targets.
    """
    schemas = spec.get("components", {}).get("schemas", {})
    if not schemas:
        return

    # Build case-insensitive lookup: lowercase → actual key
    lower_map: dict[str, str] = {}
    for key in schemas:
        lower_map.setdefault(key.lower(), key)

    # Collect all $ref targets from entire spec
    ref_targets: set[str] = set()
    prefix = "#/components/schemas/"

    def _collect(obj: object) -> None:
        if isinstance(obj, dict):
            ref = obj.get("$ref")
            if isinstance(ref, str) and ref.startswith(prefix):
                ref_targets.add(ref[len(prefix):])
            for v in obj.values():
                _collect(v)
        elif isinstance(obj, list):
            for item in obj:
                _collect(item)

    _collect(spec.get("paths", {}))
    _collect(schemas)

    # Build rename map: old_key → new_key (matching $ref target)
    renames: dict[str, str] = {}
    for target in ref_targets:
        if target not in schemas:
            actual = lower_map.get(target.lower())
            if actual and actual != target and actual not in renames:
                renames[actual] = target

    if not renames:
        return

    # Apply renames to schema keys
    for old_key, new_key in renames.items():
        logger.info("Renaming schema key '%s' → '%s' to match $ref target", old_key, new_key)
        schemas[new_key] = schemas.pop(old_key)

    # Update any $refs that pointed to old keys
    old_to_new_ref = {
        f"{prefix}{old}": f"{prefix}{new}"
        for old, new in renames.items()
    }

    def _update_refs(obj: object) -> None:
        if isinstance(obj, dict):
            if "$ref" in obj and obj["$ref"] in old_to_new_ref:
                obj["$ref"] = old_to_new_ref[obj["$ref"]]
            for v in obj.values():
                _update_refs(v)
        elif isinstance(obj, list):
            for item in obj:
                _update_refs(item)

    _update_refs(spec)


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


def _synthesize_polymorphism(spec: dict, inheritance_map: dict) -> None:
    """Add oneOf to base schemas that have subtypes present in the spec.

    When ctags reports that CreditCard and BankTransfer inherit from
    PaymentMethod, and all three are in components/schemas, this adds
    ``oneOf`` to PaymentMethod pointing to the subtypes. This tells
    pentesting tools about the polymorphic response surface.
    """
    schemas = spec.get("components", {}).get("schemas", {})
    if not schemas or not inheritance_map:
        return

    prefix = "#/components/schemas/"
    for parent_name in list(schemas.keys()):
        children = inheritance_map.get(parent_name, [])
        if not children:
            continue

        # Only include subtypes that are actually in the spec
        present_children = [c.name for c in children if c.name in schemas]
        if not present_children:
            continue

        parent_schema = schemas[parent_name]

        # Don't add oneOf if the schema already has one
        if "oneOf" in parent_schema:
            continue

        parent_schema["oneOf"] = [
            {"$ref": f"{prefix}{child}"} for child in sorted(present_children)
        ]

        # Try to detect a discriminator property: a property whose enum values
        # correspond to the subtype names (case-insensitive).
        props = parent_schema.get("properties", {})
        child_names_lower = {c.lower() for c in present_children}
        for prop_name, prop_schema in props.items():
            if isinstance(prop_schema, dict) and "enum" in prop_schema:
                enum_vals = prop_schema["enum"]
                if isinstance(enum_vals, list):
                    enum_lower = {str(v).lower() for v in enum_vals}
                    # If enum values are a superset of child names, likely discriminator
                    if child_names_lower <= enum_lower:
                        parent_schema["discriminator"] = {
                            "propertyName": prop_name,
                        }
                        logger.info(
                            "Synthesized discriminator for %s: propertyName=%s",
                            parent_name, prop_name,
                        )
                        break

        logger.info(
            "Synthesized oneOf for %s: %s",
            parent_name, present_children,
        )


def assemble_spec(
    manifest: DiscoveryManifest,
    descriptors: list[EndpointDescriptor],
    schemas: dict[str, dict],
    *,
    inheritance_map: dict | None = None,
) -> AssemblyResult:
    """Assemble a full OpenAPI 3.0 spec from pipeline artifacts.

    Args:
        manifest: Scout discovery manifest.
        descriptors: Route extractor endpoint descriptors.
        schemas: Resolved schemas from the schema loop.
        inheritance_map: Optional parent→children map from ctags inherits.

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

            # Reconcile parameter names with the normalized path template.
            # If normalize_path changed param names (e.g. stripping constraints),
            # the parameter objects must match what's in the path.
            _reconcile_path_params(path_key, ep)

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
                # Try exact match first, then case-insensitive fallback
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

    # Build case-insensitive lookup for schema names (LLMs may change casing)
    schemas_lower: dict[str, str] = {}
    for key in schemas:
        schemas_lower.setdefault(key.lower(), key)

    all_needed = _collect_transitive_refs(referenced_schemas, schemas)

    for name in all_needed:
        if name in schemas:
            spec["components"]["schemas"][name] = schemas[name]
        else:
            # Case-insensitive fallback: ref_hint says "ItemViewModel",
            # LLM extracted as "itemviewmodel"
            actual = schemas_lower.get(name.lower())
            if actual:
                logger.info(
                    "Case-insensitive schema match: %s → %s", name, actual,
                )
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

    # Post-processing passes (order matters)
    # 0a. Fix non-dict property values (LLM sometimes outputs bare strings like "string")
    if spec.get("components", {}).get("schemas"):
        _fix_non_schema_properties(spec["components"]["schemas"])
    # 0b. Fix string values that should be dicts (LLM sometimes outputs JSON strings)
    if spec.get("components", {}).get("schemas"):
        _scrub_string_schemas(spec["components"]["schemas"])
    # 1. Fix $ref + sibling keys (must come before cycle detection)
    if spec.get("components", {}).get("schemas"):
        _fix_ref_siblings(spec["components"]["schemas"])
    # 2. Break circular $ref chains
    _break_ref_cycles(spec)
    # 3. Fix case mismatches between $ref targets and schema keys
    _normalize_schema_case(spec)
    # 4. Deduplicate operationIds
    _deduplicate_operation_ids(spec)
    # 5. Synthesize polymorphism (oneOf) from inheritance map
    if inheritance_map:
        _synthesize_polymorphism(spec, inheritance_map)
    # 6. Strip empty "required" arrays (OpenAPI 3.0 violation)
    _strip_empty_required(spec)

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
