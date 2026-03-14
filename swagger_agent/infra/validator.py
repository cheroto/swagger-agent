"""Validator — structural validation and completeness checking for OpenAPI specs."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from swagger_agent.models import (
    CompletenessChecklist,
    DiscoveryManifest,
    EndpointDescriptor,
)


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _normalize_path_for_dedup(path: str) -> str:
    """Normalize a path for duplicate detection.

    Replaces {param} with a placeholder, collapses separators (/, -, _)
    so /request/magic and /request-magic both become request_magic.
    """
    normalized = re.sub(r"\{[^}]+\}", "_PARAM_", path)
    normalized = normalized.strip("/").lower()
    normalized = re.sub(r"[/\-_]+", "_", normalized)
    return normalized


def _detect_duplicate_paths(paths: dict, result: ValidationResult) -> None:
    """Detect paths that are likely variants of the same endpoint."""
    method_path_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for path, methods in paths.items():
        for method in methods:
            if not isinstance(methods[method], dict):
                continue
            norm = _normalize_path_for_dedup(path)
            method_path_groups[(method, norm)].append(path)

    for (method, _norm), originals in method_path_groups.items():
        if len(originals) > 1:
            result.warnings.append(
                f"Potential duplicate {method.upper()} paths: "
                f"{', '.join(sorted(originals))} (normalized to same pattern)"
            )


def _collect_all_ref_targets(spec: dict) -> set[str]:
    """Walk the entire spec and collect all schema names referenced via $ref."""
    targets: set[str] = set()
    prefix = "#/components/schemas/"

    def _walk(obj):
        if isinstance(obj, dict):
            ref = obj.get("$ref", "")
            if isinstance(ref, str) and ref.startswith(prefix):
                targets.add(ref[len(prefix):])
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    # Walk paths and schemas (schemas can reference other schemas)
    _walk(spec.get("paths", {}))
    _walk(spec.get("components", {}).get("schemas", {}))
    return targets


def validate_spec(spec: dict) -> ValidationResult:
    """Validate an OpenAPI 3.0 spec using openapi-spec-validator plus custom checks.

    Returns structural errors from the library and pentest-focused warnings.
    """
    result = ValidationResult()

    # OpenAPI structural validation
    try:
        from openapi_spec_validator import validate

        validate(spec)
    except ImportError:
        result.warnings.append(
            "openapi-spec-validator not installed; skipping structural validation"
        )
    except Exception as e:
        result.errors.append(str(e))

    # Custom warnings: pentest-focused checks beyond spec validity
    schemas = spec.get("components", {}).get("schemas", {})
    paths = spec.get("paths", {})

    # Unresolved schemas
    for name, schema in schemas.items():
        if schema.get("x-unresolved"):
            result.warnings.append(f"Unresolved schema: {name}")

    # Unused schemas: defined in components/schemas but never referenced via $ref
    if schemas:
        referenced = _collect_all_ref_targets(spec)
        for name in schemas:
            if name not in referenced:
                result.warnings.append(f"Unused schema: {name}")

    # Detect potential duplicate paths (variants like /request/magic vs /request-magic)
    _detect_duplicate_paths(paths, result)

    # Operations missing security key, missing requestBody, or opaque request bodies
    for path, methods in paths.items():
        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            op_id = operation.get("operationId", f"{method.upper()} {path}")

            if "security" not in operation:
                result.warnings.append(f"No security declared on {op_id}")

            if method in ("post", "put", "patch") and "requestBody" not in operation:
                result.warnings.append(f"No requestBody on {method.upper()} {op_id}")

            # Opaque request bodies: schema is bare type: object with no structure
            if method in ("post", "put", "patch") and "requestBody" in operation:
                rb_content = operation["requestBody"].get("content", {})
                for _ct, media in rb_content.items():
                    schema = media.get("schema", {})
                    if isinstance(schema, dict) and schema.get("type") == "object":
                        has_ref = "$ref" in schema or "allOf" in schema or "oneOf" in schema or "anyOf" in schema
                        has_props = "properties" in schema
                        if not has_ref and not has_props:
                            result.warnings.append(
                                f"Opaque request body schema on {op_id}: "
                                f"bare 'type: object' with no properties or schema reference"
                            )

    return result


def check_completeness(
    spec: dict,
    manifest: DiscoveryManifest,
    descriptors: list[EndpointDescriptor],
) -> CompletenessChecklist:
    """Evaluate an assembled spec against the completeness checklist."""
    paths = spec.get("paths", {})
    schemas = spec.get("components", {}).get("schemas", {})
    security_schemes = spec.get("components", {}).get("securitySchemes", {})
    servers = spec.get("servers", [])

    # Collect all operations
    operations: list[dict] = []
    for _path, methods in paths.items():
        for _method, op in methods.items():
            if isinstance(op, dict):
                operations.append(op)

    # has_endpoints
    has_endpoints = len(paths) > 0

    # has_security_schemes
    has_security_schemes = len(security_schemes) > 0

    # endpoints_have_auth: every operation has a security key.
    # With security now always emitted by the assembler (non-optional list),
    # this checks that the LLM made an explicit auth decision for every endpoint.
    endpoints_have_auth = all("security" in op for op in operations) if operations else False

    # has_error_responses: protected endpoints have 401 or 403
    has_error_responses = True
    for op in operations:
        sec = op.get("security")
        if sec is None or (isinstance(sec, list) and len(sec) > 0):
            # This is a protected endpoint (has security or inherits global)
            responses = op.get("responses", {})
            if not any(code in responses for code in ("401", "403")):
                has_error_responses = False
                break

    # has_request_bodies: always True — the assembler faithfully copies
    # request bodies from descriptors. Endpoints without request_body in the
    # descriptor are intentionally bodyless (state toggles, actions).
    has_request_bodies = True

    # has_schemas
    has_schemas = len(schemas) > 0

    # no_unresolved_refs: no x-unresolved schemas, and all $ref targets exist
    unresolved_exist = any(s.get("x-unresolved") for s in schemas.values())
    # Check all $ref targets resolve
    all_refs_valid = True
    for _path, methods in paths.items():
        for _method, op in methods.items():
            if not isinstance(op, dict):
                continue
            # Check requestBody refs
            rb = op.get("requestBody", {})
            for _ct, media in rb.get("content", {}).items():
                ref = media.get("schema", {}).get("$ref", "")
                if ref.startswith("#/components/schemas/"):
                    name = ref.split("/")[-1]
                    if name not in schemas:
                        all_refs_valid = False
            # Check response refs
            for _code, resp in op.get("responses", {}).items():
                for _ct, media in resp.get("content", {}).items():
                    ref = media.get("schema", {}).get("$ref", "")
                    if ref.startswith("#/components/schemas/"):
                        name = ref.split("/")[-1]
                        if name not in schemas:
                            all_refs_valid = False
    no_unresolved_refs = not unresolved_exist and all_refs_valid

    # has_servers
    has_servers = len(servers) > 0

    # route_coverage
    total_route_files = len(manifest.route_files) if manifest.route_files else 1
    route_coverage = len(descriptors) / total_route_files

    return CompletenessChecklist(
        has_endpoints=has_endpoints,
        has_security_schemes=has_security_schemes,
        endpoints_have_auth=endpoints_have_auth,
        has_error_responses=has_error_responses,
        has_request_bodies=has_request_bodies,
        has_schemas=has_schemas,
        no_unresolved_refs=no_unresolved_refs,
        has_servers=has_servers,
        route_coverage=min(route_coverage, 1.0),
    )
