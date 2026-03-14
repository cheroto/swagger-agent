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


def _synthesize_polymorphism(spec: dict, inheritance_map: dict) -> None:
    """Add oneOf to base schemas that have subtypes present in the spec."""
    schemas = spec.get("components", {}).get("schemas", {})
    if not schemas or not inheritance_map:
        return

    prefix = "#/components/schemas/"
    for parent_name in list(schemas.keys()):
        children = inheritance_map.get(parent_name, [])
        if not children:
            continue

        present_children = [c.name for c in children if c.name in schemas]
        if not present_children:
            continue

        parent_schema = schemas[parent_name]

        if "oneOf" in parent_schema:
            continue

        parent_schema["oneOf"] = [
            {"$ref": f"{prefix}{child}"} for child in sorted(present_children)
        ]

        discriminator_found = False
        props = parent_schema.get("properties", {})
        child_names_lower = {c.lower() for c in present_children}

        # Strategy 1: parent enum
        for prop_name, prop_schema in props.items():
            if isinstance(prop_schema, dict) and "enum" in prop_schema:
                enum_vals = prop_schema["enum"]
                if isinstance(enum_vals, list):
                    enum_lower = {str(v).lower() for v in enum_vals}
                    if child_names_lower <= enum_lower:
                        parent_schema["discriminator"] = {
                            "propertyName": prop_name,
                        }
                        logger.info(
                            "Synthesized discriminator for %s: propertyName=%s (parent enum)",
                            parent_name, prop_name,
                        )
                        discriminator_found = True
                        break

        # Strategy 2: children share a property with distinct constant values
        if not discriminator_found and len(present_children) >= 2:
            child_schemas = [schemas.get(c, {}) for c in present_children]
            child_prop_sets = [
                set(cs.get("properties", {}).keys()) for cs in child_schemas
            ]
            if child_prop_sets:
                shared_props = child_prop_sets[0]
                for ps in child_prop_sets[1:]:
                    shared_props &= ps

                for prop_name in shared_props:
                    child_values: list[str] = []
                    all_constant = True
                    for cs in child_schemas:
                        cp = cs.get("properties", {}).get(prop_name, {})
                        if isinstance(cp, dict):
                            enum_vals = cp.get("enum", [])
                            const_val = cp.get("const")
                            if const_val is not None:
                                child_values.append(str(const_val))
                            elif isinstance(enum_vals, list) and len(enum_vals) == 1:
                                child_values.append(str(enum_vals[0]))
                            else:
                                all_constant = False
                                break
                        else:
                            all_constant = False
                            break

                    if all_constant and len(set(child_values)) == len(present_children):
                        parent_schema["discriminator"] = {
                            "propertyName": prop_name,
                        }
                        logger.info(
                            "Synthesized discriminator for %s: propertyName=%s "
                            "(children constants: %s)",
                            parent_name, prop_name, child_values,
                        )
                        break

        logger.info(
            "Synthesized oneOf for %s: %s",
            parent_name, present_children,
        )
