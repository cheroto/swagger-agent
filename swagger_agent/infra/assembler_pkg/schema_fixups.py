"""Schema post-processing passes for cleaning up LLM-produced schemas."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict

logger = logging.getLogger("swagger_agent.assembler")


_JSON_SCHEMA_TYPES = frozenset({
    "string", "integer", "number", "boolean", "array", "object",
})

# Map well-known type names (from any language) to inline JSON Schema types.
# When a $ref targets one of these, we inline the type instead of creating
# an unresolved placeholder. Case-insensitive lookup.
_OPAQUE = {"type": "object"}

_PRIMITIVE_TYPE_MAP: dict[str, dict] = {
    "string": {"type": "string"}, "str": {"type": "string"},
    "integer": {"type": "integer"}, "int": {"type": "integer"},
    "long": {"type": "integer", "format": "int64"},
    "float": {"type": "number", "format": "float"},
    "double": {"type": "number", "format": "double"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"}, "bool": {"type": "boolean"},
    "object": _OPAQUE, "dict": _OPAQUE, "map": _OPAQUE, "any": {},
    "void": {"type": "object", "description": "void"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
    "uuid": {"type": "string", "format": "uuid"},
    "uri": {"type": "string", "format": "uri"},
    "byte": {"type": "string", "format": "byte"},
    "binary": {"type": "string", "format": "binary"},
    # Framework HTTP/response infrastructure types — no user-defined schema.
    # These are inlined as {type: "object"} instead of creating unresolvable $refs.
    "iactionresult": _OPAQUE, "actionresult": _OPAQUE,
    "ihttpactionresult": _OPAQUE,
    "httpresponsemessage": _OPAQUE, "httpresponse": _OPAQUE,
    "httprequestmessage": _OPAQUE,
    "iformfile": _OPAQUE, "formfile": _OPAQUE,
    "cancellationtoken": _OPAQUE,
    "healthcheckresult": _OPAQUE,
    "fileresult": _OPAQUE, "jsonresult": _OPAQUE,
    "viewresult": _OPAQUE, "contentresult": _OPAQUE,
    "statuscoderesult": _OPAQUE, "objectresult": _OPAQUE,
    "responseentity": _OPAQUE,
    "httpservletrequest": _OPAQUE, "httpservletresponse": _OPAQUE,
    "modelandview": _OPAQUE, "redirectview": _OPAQUE,
    "validationproblemdetails": _OPAQUE, "problemdetails": _OPAQUE,
}


def primitive_schema(name: str) -> dict | None:
    """Return an inline JSON Schema for a primitive type name, or None.

    Also handles comma-separated builtins like "str, Any" — if all parts
    are primitives, returns a generic object schema.
    """
    result = _PRIMITIVE_TYPE_MAP.get(name.lower())
    if result is not None:
        return result
    # Handle comma-separated type args that leaked as schema names
    # e.g. "str, Any" from Dict[str, Any] when the LLM emits it as a $ref
    if ", " in name:
        parts = [p.strip() for p in name.split(",")]
        if all(p.lower() in _PRIMITIVE_TYPE_MAP for p in parts):
            return {"type": "object"}
    return None


def _fix_leaked_ref_hints(obj: object) -> None:
    """Convert raw RefHint dicts that leaked into schema positions to $refs.

    When the LLM puts a RefHint object (with keys like ref_hint, resolution,
    import_line) as a parameter schema instead of a proper JSON Schema, this
    converts it to a valid $ref.
    """
    if isinstance(obj, dict):
        # Check if this dict looks like a RefHint (has ref_hint key, no type key)
        if "ref_hint" in obj and "type" not in obj and "$ref" not in obj:
            hint_name = obj["ref_hint"]
            # Clear all ref_hint keys and replace with $ref
            for key in list(obj.keys()):
                del obj[key]
            obj["$ref"] = f"#/components/schemas/{hint_name}"
            return
        for v in obj.values():
            _fix_leaked_ref_hints(v)
    elif isinstance(obj, list):
        for item in obj:
            _fix_leaked_ref_hints(item)


def inline_primitive_refs(obj: object) -> None:
    """Replace $ref to primitive types with inline JSON Schema.

    When the LLM emits $ref: '#/components/schemas/String', this replaces
    it with {type: "string"} inline, avoiding unresolved placeholders.
    """
    prefix = "#/components/schemas/"
    if isinstance(obj, dict):
        ref = obj.get("$ref")
        if isinstance(ref, str) and ref.startswith(prefix):
            type_name = ref[len(prefix):]
            inline = primitive_schema(type_name)
            if inline is not None:
                del obj["$ref"]
                obj.update(inline)
                return
        for v in obj.values():
            inline_primitive_refs(v)
    elif isinstance(obj, list):
        for item in obj:
            inline_primitive_refs(item)

_STRING_VALUE_KEYS = frozenset({
    "$ref", "type", "format", "description", "pattern",
    "title", "default", "example", "x-circular-ref",
})

_CONSTRAINT_RENAMES: dict[str, str] = {
    "ge": "minimum", "le": "maximum",
    "gt": "exclusiveMinimum", "lt": "exclusiveMaximum",
    "multiple_of": "multipleOf",
    "min_length": "minLength", "max_length": "maxLength",
    "minlength": "minLength", "maxlength": "maxLength",
    "min_items": "minItems", "max_items": "maxItems",
    "minitems": "minItems", "maxitems": "maxItems",
    "unique_items": "uniqueItems",
    "min_properties": "minProperties", "max_properties": "maxProperties",
}


def _coerce_to_schema(value: object) -> dict:
    """Convert a non-dict value into a valid JSON Schema object."""
    if isinstance(value, str):
        stripped = value.strip()
        low = stripped.lower()
        if low in _JSON_SCHEMA_TYPES:
            return {"type": low}
        if stripped.startswith("#/components/schemas/"):
            return {"$ref": stripped}
        if (stripped.startswith("{") and stripped.endswith("}")) or \
           (stripped.startswith("[") and stripped.endswith("]")):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
        if stripped and stripped[0].isupper() and stripped.isidentifier():
            return {"$ref": f"#/components/schemas/{stripped}"}
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


def _sanitize_schemas(obj: object) -> None:
    """Single-pass cleanup of LLM-produced schema dicts.

    Combines four concerns into one DFS traversal:
    1. Rename non-standard constraint keywords (ge→minimum, etc.)
    2. Fix string values that should be dicts (JSON strings, bare $refs)
    3. Coerce non-dict property values to valid schema objects
    4. Strip empty 'required' arrays (OpenAPI 3.0 violation)
    """
    if isinstance(obj, dict):
        for old_key, new_key in _CONSTRAINT_RENAMES.items():
            if old_key in obj and new_key not in obj:
                obj[new_key] = obj.pop(old_key)

        # Convert JSON Schema 2020 number-form exclusiveMinimum/Maximum to
        # OpenAPI 3.0 boolean-form: exclusiveMinimum: 5 → minimum: 5, exclusiveMinimum: true
        for exc_key, min_key in [("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum")]:
            if exc_key in obj and isinstance(obj[exc_key], (int, float)):
                obj[min_key] = obj.pop(exc_key)
                obj[exc_key] = True

        for k, v in list(obj.items()):
            if isinstance(v, str) and k not in _STRING_VALUE_KEYS:
                stripped = v.strip()
                if (stripped.startswith("{") and stripped.endswith("}")) or \
                   (stripped.startswith("[") and stripped.endswith("]")):
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, (dict, list)):
                            obj[k] = parsed
                    except (json.JSONDecodeError, ValueError):
                        pass
                elif stripped.startswith("#/components/schemas/"):
                    obj[k] = {"$ref": stripped}

        if "properties" in obj and isinstance(obj["properties"], dict):
            for key, val in list(obj["properties"].items()):
                if not isinstance(val, dict):
                    obj["properties"][key] = _coerce_to_schema(val)

        if "required" in obj and obj["required"] == []:
            del obj["required"]

        # Fix minimum/maximum on string types — should be minLength/maxLength.
        # LLMs often confuse numeric constraints with string length constraints.
        if obj.get("type") == "string":
            if "minimum" in obj and "minLength" not in obj:
                obj["minLength"] = int(obj.pop("minimum"))
            if "maximum" in obj and "maxLength" not in obj:
                obj["maxLength"] = int(obj.pop("maximum"))

        for v in obj.values():
            _sanitize_schemas(v)
    elif isinstance(obj, list):
        for item in obj:
            _sanitize_schemas(item)


def _fix_ref_siblings(schema: object) -> object:
    """Wrap $ref + sibling keys with allOf (OpenAPI 3.0 requires it)."""
    if isinstance(schema, dict):
        if "$ref" in schema and len(schema) > 1:
            ref = schema.pop("$ref")
            schema["allOf"] = [{"$ref": ref}]
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


def _break_ref_cycles(spec: dict) -> None:
    """Detect and break circular $ref chains in components/schemas."""
    schemas = spec.get("components", {}).get("schemas", {})
    if not schemas:
        return

    graph: dict[str, set[str]] = {}
    array_edges: set[tuple[str, str]] = set()
    for name, schema in schemas.items():
        refs = _extract_refs_from_schema(schema)
        graph[name] = refs
        _mark_array_edges(name, schema, array_edges)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in schemas}
    parent: dict[str, str | None] = {n: None for n in schemas}
    back_edges: list[tuple[str, str]] = []

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

    for src, tgt in back_edges:
        if (src, tgt) in array_edges:
            _replace_ref_in_schema(schemas[src], tgt)
        elif (tgt, src) in array_edges:
            _replace_ref_in_schema(schemas[tgt], src)
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


def _normalize_schema_case(spec: dict) -> None:
    """Fix case mismatches between $ref targets and schema keys."""
    schemas = spec.get("components", {}).get("schemas", {})
    if not schemas:
        return

    lower_map: dict[str, str] = {}
    for key in schemas:
        lower_map.setdefault(key.lower(), key)

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

    renames: dict[str, str] = {}
    for target in ref_targets:
        if target not in schemas:
            actual = lower_map.get(target.lower())
            if actual and actual != target and actual not in renames:
                renames[actual] = target

    if not renames:
        return

    for old_key, new_key in renames.items():
        logger.info("Renaming schema key '%s' → '%s' to match $ref target", old_key, new_key)
        schemas[new_key] = schemas.pop(old_key)

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
    id_to_locations: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if isinstance(op, dict) and "operationId" in op:
                id_to_locations[op["operationId"]].append((path, method))

    for op_id, locations in id_to_locations.items():
        if len(locations) <= 1:
            continue
        for path, method in locations:
            op = spec["paths"][path][method]
            tags = op.get("tags", [])
            tag = tags[0] if tags else ""
            new_id = f"{tag}_{op_id}" if tag else f"{path.strip('/').replace('/', '_')}_{op_id}"
            op["operationId"] = new_id

        seen: set[str] = set()
        for path, method in locations:
            op = spec["paths"][path][method]
            if op["operationId"] in seen:
                h = hashlib.md5(f"{path}:{method}".encode()).hexdigest()[:6]
                op["operationId"] = f"{op['operationId']}_{h}"
            seen.add(op["operationId"])


