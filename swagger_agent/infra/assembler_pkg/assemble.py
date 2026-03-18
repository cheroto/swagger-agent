"""Core spec assembly — builds OpenAPI 3.0 from pipeline artifacts."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import yaml

from swagger_agent.models import DiscoveryManifest, EndpointDescriptor, Endpoint, SecurityRequirement

from .path_utils import _normalize_path, extract_path_params, normalize_path_template
from .schema_fixups import (
    _break_ref_cycles,
    _deduplicate_operation_ids,
    _extract_refs_from_schema,
    _fix_array_missing_items,
    _fix_leaked_ref_hints,
    _fix_ref_siblings,
    _normalize_schema_case,
    _sanitize_schemas,
    inline_primitive_refs,
    primitive_schema,
)

logger = logging.getLogger("swagger_agent.assembler")


@dataclass
class AssemblyResult:
    spec: dict
    yaml_str: str


# ── Ref hint parsing ─────────────────────────────────────────────────────

# Bracket-only array patterns (no named generics — those are handled separately)
_BRACKET_ARRAY_RE = re.compile(
    r"^(?:"
    r"\[\](.+)"           # Go:      []Type
    r"|(.+)\[\]"          # TS/Java: Type[]
    r")$"
)

# Generic pattern: Wrapper[Inner] or Wrapper<Inner>
_GENERIC_RE = re.compile(r"^(\w+)\s*[\[<](.+)[\]>]$")

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


_SPACE_COLLECTION_SUFFIXES = {"list", "array", "option", "seq", "set", "ref"}

_COLLECTION_WRAPPERS = frozenset({
    "List", "list", "Array", "array", "Set", "set", "FrozenSet", "frozenset",
    "Sequence", "sequence", "Iterable", "iterable",
    "IEnumerable", "IList", "ICollection", "ISet",
    "Collection", "Vec", "vector", "Deque", "deque", "Queue",
    "HashSet", "TreeSet", "LinkedList", "ArrayList",
})

_MAP_WRAPPERS = frozenset({
    "Map", "map", "Dict", "dict", "HashMap", "hashMap",
    "Mapping", "OrderedDict", "defaultdict", "TreeMap", "LinkedHashMap",
    "Record", "Object",
})


def _parse_ref_hint(name: str) -> tuple[bool, str]:
    """Parse a ref_hint, detecting array wrappers, maps, and response wrappers.

    Returns (is_array, inner_type_name).

    - Known collection wrappers (List<T>, IEnumerable<T>) → (True, T)
    - Known map wrappers (Map<K,V>, Dict[K,V]) → (False, "")
    - Unknown wrappers (ActionResult<T>, Task<T>) → (False, T)  (pass-through)
    - Bracket syntax ([]T, T[]) → (True, T)
    """
    name = _sanitize_ref_hint(name)
    m = re.match(r"^Optional\[(.+)\]$", name)
    if m:
        name = m.group(1).strip()

    # Bracket array syntax: []Type or Type[]
    m = _BRACKET_ARRAY_RE.match(name)
    if m:
        inner = next(g for g in m.groups() if g is not None)
        return True, inner.strip()

    # Named generic: Wrapper[Inner] or Wrapper<Inner>
    m = _GENERIC_RE.match(name)
    if m:
        wrapper = m.group(1)
        inner = m.group(2).strip()
        if wrapper in _MAP_WRAPPERS:
            return False, ""  # map → plain object
        if wrapper in _COLLECTION_WRAPPERS:
            return True, inner  # collection → array
        # Unknown wrapper (ActionResult<T>, Task<T>, etc.) → pass-through to inner
        return False, inner

    # ML-family space-suffix collections: "Reading.t list" → array of Reading.t
    parts = name.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].lower() in _SPACE_COLLECTION_SUFFIXES:
        return True, parts[0].strip()

    # Bare comma-separated types: "String, dynamic" → take first non-empty
    if "," in name and "[" not in name and "<" not in name:
        first = name.split(",")[0].strip()
        return False, first if first else name

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
    """Build the schema dict for a RefHint, applying name rewrites if any.

    Uses structured is_array/is_nullable fields when set, falling through
    to _build_ref's regex-based parsing as a safety net.
    """
    name = ref_hint_obj.ref_hint
    if ref_rewrite:
        name = ref_rewrite.get(name, name)

    # Structured fields from the LLM take priority over regex parsing
    if getattr(ref_hint_obj, "is_array", False) or getattr(ref_hint_obj, "is_nullable", False):
        inner = _build_ref(name)
        if getattr(ref_hint_obj, "is_array", False):
            inner = {"type": "array", "items": inner}
        if getattr(ref_hint_obj, "is_nullable", False):
            # OAS 3.0: nullable must be on a node with 'type'. If the node is a
            # $ref, wrap in allOf so nullable sits on the wrapper object.
            if "$ref" in inner:
                inner = {"allOf": [{"$ref": inner.pop("$ref")}], "nullable": True}
            else:
                inner["nullable"] = True
        return inner

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
    # Primitive/framework types get inlined by inline_primitive_refs later.
    # We still create the $ref here; the postprocessor replaces it.
    if is_array:
        return {
            "type": "array",
            "items": {"$ref": f"#/components/schemas/{inner}"},
        }
    return {"$ref": f"#/components/schemas/{inner}"}


# ── Operation / security helpers ─────────────────────────────────────────

def _scheme_type_to_openapi(req: SecurityRequirement) -> dict:
    """Map a SecurityRequirement to an OpenAPI securitySchemes entry.

    Uses enriched fields (oauth2_flow, token_url, scopes, apikey_name, etc.)
    when populated; falls back to sensible defaults when fields are empty.
    """
    scheme_type = req.scheme_type

    if scheme_type == "bearer":
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}

    if scheme_type == "apikey":
        return {
            "type": "apiKey",
            "in": req.apikey_in,
            "name": req.apikey_name or "X-API-Key",
        }

    if scheme_type == "basic":
        return {"type": "http", "scheme": "basic"}

    if scheme_type == "oauth2":
        scopes_dict = {s: "" for s in req.scopes} if req.scopes else {}
        flow = req.oauth2_flow

        if flow == "authorizationCode":
            flow_obj = {
                "authorizationCode": {
                    "authorizationUrl": "",
                    "tokenUrl": "",
                    "scopes": scopes_dict,
                }
            }
        elif flow == "clientCredentials":
            flow_obj = {
                "clientCredentials": {
                    "tokenUrl": "",
                    "scopes": scopes_dict,
                }
            }
        elif flow == "implicit":
            flow_obj = {
                "implicit": {
                    "authorizationUrl": "",
                    "scopes": scopes_dict,
                }
            }
        else:
            # Default to password flow (most permissive)
            flow_obj = {
                "password": {
                    "tokenUrl": "",
                    "scopes": scopes_dict,
                }
            }

        return {"type": "oauth2", "flows": flow_obj}

    if scheme_type == "cookie":
        return {"type": "apiKey", "in": "cookie", "name": "session"}

    # Fallback for unknown types
    return {"type": "http", "scheme": "bearer"}


def _build_operation(
    ep: Endpoint,
    path_key: str,
    ref_rewrite: dict[str, str] | None = None,
) -> dict:
    """Build an OpenAPI operation object from an Endpoint model.

    Path parameters are derived from {param} segments in path_key.
    The LLM only outputs query/header/cookie params in ep.parameters.
    """
    op: dict = {"operationId": ep.operation_id}

    if ep.tags:
        op["tags"] = ep.tags

    # Derive path params from the path template (infrastructure, not LLM)
    path_param_names = extract_path_params(path_key)
    path_param_name_set = set(path_param_names)

    # Build parameters: path params first (from path), then LLM params
    # Filter out any LLM params whose name collides with a path param
    params: list[dict] = []
    for name in path_param_names:
        params.append({"name": name, "in": "path", "required": True, "schema": {"type": "string"}})

    if ep.parameters:
        seen: set[tuple[str, str]] = {(name, "path") for name in path_param_names}
        for p in ep.parameters:
            dumped = p.model_dump(by_alias=True, exclude_none=True)
            # Skip if name collides with a path param
            if dumped["name"] in path_param_name_set:
                continue
            key = (dumped["name"], dumped["in"])
            if key not in seen:
                seen.add(key)
                params.append(dumped)

    if params:
        op["parameters"] = params

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
            if not resp.schema_ref.is_empty:
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
        op["security"] = [{req.name: req.scopes if req.scopes else []} for req in ep.security]

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

    # Security schemes — collect SecurityRequirement objects from all endpoints
    all_schemes: dict[str, SecurityRequirement] = {}
    for desc in descriptors:
        for ep in desc.endpoints:
            for req in ep.security:
                if req.name not in all_schemes:
                    all_schemes[req.name] = req

    for name in sorted(all_schemes):
        req = all_schemes[name]
        spec["components"]["securitySchemes"][name] = _scheme_type_to_openapi(req)

    if not spec["components"]["securitySchemes"]:
        del spec["components"]["securitySchemes"]

    # Paths and endpoints
    referenced_schemas: set[str] = set()

    # Track normalized path templates to detect OAS-identical paths
    # (e.g. /api/{slug} and /api/{id} are identical in OAS 3.0)
    norm_to_canonical: dict[str, str] = {}

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

            # Detect OAS-identical paths (same template, different param names)
            norm_key = normalize_path_template(path_key)
            if norm_key in norm_to_canonical and norm_to_canonical[norm_key] != path_key:
                canonical = norm_to_canonical[norm_key]
                logger.warning(
                    "Path '%s' is identical to '%s' in OAS 3.0 (param names differ) — merging into '%s'",
                    path_key, canonical, canonical,
                )
                path_key = canonical
            else:
                norm_to_canonical[norm_key] = path_key

            if path_key not in spec["paths"]:
                spec["paths"][path_key] = {}

            # Skip duplicate methods on the same path (keep first)
            if method in spec["paths"][path_key]:
                logger.warning(
                    "Duplicate %s %s (operationId=%s) — keeping first, skipping duplicate",
                    method.upper(), path_key, ep.operation_id,
                )
                continue

            op = _build_operation(ep, path_key, ref_rewrite)

            # Auto-inject 401/403 for protected endpoints missing error responses
            if ep.security and len(ep.security) > 0:
                responses = op.get("responses", {})
                if "401" not in responses:
                    responses["401"] = {"description": "Unauthorized"}
                if "403" not in responses:
                    responses["403"] = {"description": "Forbidden"}
                op["responses"] = responses

            spec["paths"][path_key][method] = op

            # Track referenced schema names (using rewritten names)
            for ref_source in (
                [ep.request_body.schema_ref] if ep.request_body and ep.request_body.schema_ref else []
            ) + [resp.schema_ref for resp in ep.responses if not resp.schema_ref.is_empty]:
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
    _fix_array_missing_items(spec)
    _normalize_schema_case(spec)
    _deduplicate_operation_ids(spec)
    if not spec["components"]["schemas"]:
        del spec["components"]["schemas"]
    if not spec.get("components"):
        del spec["components"]

    class _NoAliasDumper(yaml.SafeDumper):
        """Prevent YAML anchors/aliases — OpenAPI validators reject them."""
        def ignore_aliases(self, data):
            return True

    yaml_str = yaml.dump(
        spec,
        Dumper=_NoAliasDumper,
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
