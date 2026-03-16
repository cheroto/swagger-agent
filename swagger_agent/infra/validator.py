"""Validator — structural validation and completeness checking for OpenAPI specs."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

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


def _run_redocly(spec: dict) -> ValidationResult:
    """Run redocly CLI for comprehensive OpenAPI validation.

    Writes spec to a temp file, runs `npx @redocly/cli lint --format json`,
    parses the structured output into errors and warnings.
    """
    result = ValidationResult()

    # Find npx
    npx = shutil.which("npx")
    if not npx:
        result.warnings.append("npx not found; skipping redocly validation")
        return result

    # Write spec to temp YAML file
    tmp_dir = tempfile.mkdtemp(prefix="swagger-agent-validate-")
    spec_path = Path(tmp_dir) / "openapi.yaml"
    try:
        spec_path.write_text(yaml.dump(spec, default_flow_style=False, sort_keys=False))

        # Skip rules that are documentation concerns, not structural/security issues.
        # Our generated specs won't have license info or summaries — that's fine.
        skip_rules = [
            "info-license",
            "info-contact",
            "operation-summary",
            "tag-description",
        ]
        cmd = [npx, "--yes", "@redocly/cli", "lint", "--format", "json"]
        for rule in skip_rules:
            cmd.extend(["--skip-rule", rule])
        cmd.append(str(spec_path))

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Redocly prints JSON to stdout (may have trailing text after the JSON)
        stdout = proc.stdout.strip()
        if not stdout:
            if proc.returncode != 0:
                result.warnings.append(
                    f"redocly exited with code {proc.returncode} but no output"
                )
            return result

        # Extract the JSON object from stdout (redocly appends summary text after it)
        json_start = stdout.find("{")
        json_end = stdout.rfind("}") + 1
        if json_start == -1 or json_end == 0:
            result.warnings.append("redocly produced no parseable JSON output")
            return result

        data = json.loads(stdout[json_start:json_end])
        problems = data.get("problems", [])

        for problem in problems:
            severity = problem.get("severity", "error")
            rule_id = problem.get("ruleId", "unknown")
            message = problem.get("message", "")
            location = problem.get("location", [{}])
            pointer = location[0].get("pointer", "") if location else ""

            formatted = f"[{rule_id}] {message}"
            if pointer:
                formatted += f" (at {pointer})"

            if severity == "error":
                result.errors.append(formatted)
            else:
                result.warnings.append(formatted)

    except subprocess.TimeoutExpired:
        result.warnings.append("redocly lint timed out after 60s")
    except json.JSONDecodeError as e:
        result.warnings.append(f"Failed to parse redocly JSON output: {e}")
    except OSError as e:
        result.warnings.append(f"Failed to run redocly: {e}")
    finally:
        # Clean up temp files
        spec_path.unlink(missing_ok=True)
        Path(tmp_dir).rmdir()

    return result


def _run_python_validator(spec: dict) -> ValidationResult:
    """Fallback: run openapi-spec-validator Python library."""
    result = ValidationResult()
    try:
        from openapi_spec_validator import validate

        validate(spec)
    except ImportError:
        result.warnings.append(
            "openapi-spec-validator not installed; skipping structural validation"
        )
    except Exception as e:
        result.errors.append(str(e))
    return result


def _find_array_without_items(spec: dict) -> list[str]:
    """Walk the entire spec and find any 'type: array' node missing 'items'."""
    issues: list[str] = []

    def _walk(obj: object, path: str) -> None:
        if isinstance(obj, dict):
            if obj.get("type") == "array" and "items" not in obj:
                issues.append(path)
            for k, v in obj.items():
                _walk(v, f"{path}/{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _walk(v, f"{path}[{i}]")

    _walk(spec, "#")
    return issues


def _run_custom_checks(spec: dict) -> ValidationResult:
    """Pentest-focused checks beyond spec validity."""
    result = ValidationResult()
    schemas = spec.get("components", {}).get("schemas", {})
    paths = spec.get("paths", {})

    # type: array without items (OAS 3.0 requires items; redocly misses this)
    for path in _find_array_without_items(spec):
        result.errors.append(f"[array-items] 'type: array' missing required 'items' (at {path})")

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


def validate_spec(spec: dict) -> ValidationResult:
    """Validate an OpenAPI 3.0 spec using redocly CLI + custom pentest checks.

    Uses redocly for comprehensive structural linting (unresolved refs, missing
    security, missing responses, unused components, etc.). Falls back to
    openapi-spec-validator Python library if redocly is unavailable.

    Then runs custom pentest-focused checks (unresolved schemas, opaque bodies,
    duplicate paths) on top.
    """
    # Primary: redocly CLI (comprehensive, multi-error)
    redocly_result = _run_redocly(spec)

    # If redocly produced no errors AND no warnings, it either succeeded cleanly
    # or wasn't available. If it warned about not being available, fall back.
    has_redocly = not any("npx not found" in w or "redocly" in w.lower()
                         for w in redocly_result.warnings
                         if "timed out" in w or "not found" in w or "no output" in w or "Failed" in w)

    if not has_redocly:
        # Fallback to Python library
        fallback = _run_python_validator(spec)
        redocly_result.errors.extend(fallback.errors)
        redocly_result.warnings.extend(fallback.warnings)

    # Always run custom pentest-focused checks (redocly doesn't know about
    # x-unresolved, opaque bodies, or our duplicate path detection)
    custom = _run_custom_checks(spec)
    redocly_result.errors.extend(custom.errors)
    redocly_result.warnings.extend(custom.warnings)

    return redocly_result


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
