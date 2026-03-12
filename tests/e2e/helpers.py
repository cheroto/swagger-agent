"""Assertion helpers for LLM-based agent e2e tests.

Since LLM output varies between runs, these helpers use structural assertions
(correct endpoints found, correct properties present) rather than exact JSON match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from swagger_agent.models import CodeAnalysis, Endpoint, EndpointDescriptor


@dataclass
class ExpectedEndpoint:
    """Golden expectation for a single endpoint."""

    method: str
    path: str  # Normalized path with {param} style
    has_auth: bool | None = None  # True=must have auth, False=must be public, None=don't check
    has_request_body: bool | None = None
    param_names: list[str] = field(default_factory=list)  # Expected parameter names
    min_responses: int = 1


@dataclass
class ExpectedPhase1Endpoint:
    """Golden expectation for a Phase 1 endpoint sketch."""

    method: str
    path: str  # Normalized path with {param} style
    handler_name: str | None = None  # Optional — check if non-None


@dataclass
class Phase1Golden:
    """Golden data for Phase 1 (code analysis) output."""

    min_endpoints: int
    endpoints: list[ExpectedPhase1Endpoint] = field(default_factory=list)
    has_auth_patterns: bool | None = None  # True=must have, False=must be empty, None=don't check
    has_auth_imports: bool | None = None
    has_auth_inference_notes: bool | None = None  # True=must be non-empty
    base_prefix: str | None = None  # Expected base prefix, None=don't check
    path_param_syntax: str | None = None  # Expected syntax, None=don't check
    required_import_substrings: list[str] = field(default_factory=list)  # Substrings that must appear in at least one import line


@dataclass
class RouteGolden:
    """Golden data for a route extraction test case."""

    repo_id: str
    repo_dir: str  # Relative to REPOS_ROOT
    route_file: str  # Relative to repo_dir
    framework: str
    base_path: str
    min_endpoints: int
    endpoints: list[ExpectedEndpoint]
    phase1: Phase1Golden | None = None  # Optional Phase 1 assertions


@dataclass
class ExpectedSchema:
    """Golden expectation for a single schema."""

    name: str
    min_properties: int = 1
    expected_properties: list[str] = field(default_factory=list)
    expected_required: list[str] = field(default_factory=list)


@dataclass
class SchemaLoopGolden:
    """Golden data for a schema loop test case."""

    repo_id: str
    repo_dir: str
    framework: str
    ref_hints: list[dict]
    min_schemas: int
    expected_schemas: list[ExpectedSchema]


def assert_phase1_match(
    analysis: CodeAnalysis,
    golden: Phase1Golden,
    repo_id: str,
) -> None:
    """Assert that Phase 1 code analysis matches golden expectations."""
    sketches = analysis.endpoints

    assert len(sketches) >= golden.min_endpoints, (
        f"[{repo_id}] Phase 1: expected at least {golden.min_endpoints} endpoint sketches, "
        f"got {len(sketches)}: {[(s.method, s.path) for s in sketches]}"
    )

    for expected in golden.endpoints:
        # For Phase 1, use exact path match (normalized) since sketches
        # should have the same path format as golden data
        expected_norm = normalize_path(expected.path)

        match = None
        for s in sketches:
            norm = normalize_path(s.path)
            if s.method.upper() == expected.method.upper() and norm == expected_norm:
                match = s
                break
        assert match is not None, (
            f"[{repo_id}] Phase 1: expected sketch {expected.method} {expected.path} "
            f"not found. Got: {[(s.method, normalize_path(s.path)) for s in sketches]}"
        )
        if expected.handler_name is not None:
            assert match.handler_name == expected.handler_name, (
                f"[{repo_id}] Phase 1: {expected.method} {expected.path} "
                f"expected handler '{expected.handler_name}', got '{match.handler_name}'"
            )

    if golden.has_auth_patterns is True:
        assert len(analysis.auth_patterns) > 0, (
            f"[{repo_id}] Phase 1: expected auth patterns, got none"
        )
    elif golden.has_auth_patterns is False:
        assert len(analysis.auth_patterns) == 0, (
            f"[{repo_id}] Phase 1: expected no auth patterns, "
            f"got: {[(ap.indicator, ap.applies_to) for ap in analysis.auth_patterns]}"
        )

    if golden.has_auth_imports is not None:
        assert analysis.has_auth_imports == golden.has_auth_imports, (
            f"[{repo_id}] Phase 1: expected has_auth_imports={golden.has_auth_imports}, "
            f"got {analysis.has_auth_imports}"
        )

    if golden.has_auth_inference_notes is True:
        assert analysis.auth_inference_notes and len(analysis.auth_inference_notes.strip()) > 0, (
            f"[{repo_id}] Phase 1: expected non-empty auth_inference_notes, "
            f"got {analysis.auth_inference_notes!r}"
        )

    if golden.base_prefix is not None:
        assert analysis.base_prefix == golden.base_prefix, (
            f"[{repo_id}] Phase 1: expected base_prefix={golden.base_prefix!r}, "
            f"got {analysis.base_prefix!r}"
        )

    if golden.path_param_syntax is not None:
        assert golden.path_param_syntax in analysis.path_param_syntax, (
            f"[{repo_id}] Phase 1: expected path_param_syntax containing "
            f"{golden.path_param_syntax!r}, got {analysis.path_param_syntax!r}"
        )

    for substring in golden.required_import_substrings:
        found = any(substring in line for line in analysis.import_lines)
        assert found, (
            f"[{repo_id}] Phase 1: no import line contains {substring!r}. "
            f"Got: {analysis.import_lines}"
        )


def normalize_path(path: str) -> str:
    """Normalize path parameters: :param -> {param}, <param> -> {param}."""
    path = re.sub(r":(\w+)", r"{\1}", path)
    path = re.sub(r"<(\w+)>", r"{\1}", path)
    return path


def find_endpoint(
    endpoints: list[Endpoint], method: str, path_pattern: str
) -> Endpoint | None:
    """Find an endpoint matching method and path pattern (regex).

    Path params are normalized to {param} style before matching.
    """
    for ep in endpoints:
        if ep.method.upper() != method.upper():
            continue
        normalized = normalize_path(ep.path)
        if re.search(path_pattern, normalized):
            return ep
    return None


def assert_endpoints_match(
    descriptor: EndpointDescriptor,
    golden: RouteGolden,
) -> None:
    """Assert that extracted endpoints match golden expectations.

    Checks:
    - At least min_endpoints found
    - Each expected endpoint exists (method + path pattern)
    - Auth, request body, and parameter expectations match
    """
    endpoints = descriptor.endpoints

    assert len(endpoints) >= golden.min_endpoints, (
        f"[{golden.repo_id}] Expected at least {golden.min_endpoints} endpoints, "
        f"got {len(endpoints)}: {[(e.method, e.path) for e in endpoints]}"
    )

    for expected in golden.endpoints:
        # Build a regex pattern from the expected path
        # Replace {param} with a pattern that matches {any_param_name} but NOT
        # literal path segments like "versions"
        pattern = re.escape(expected.path)
        # Match {word} in the actual path — the LLM might use different param names
        pattern = re.sub(r"\\{[^}]+\\}", r"\\{\\w+\\}", pattern)
        pattern = f"^{pattern}$"

        ep = find_endpoint(endpoints, expected.method, pattern)
        assert ep is not None, (
            f"[{golden.repo_id}] Expected endpoint {expected.method} {expected.path} "
            f"not found. Got: {[(e.method, normalize_path(e.path)) for e in endpoints]}"
        )

        # Check auth
        if expected.has_auth is True:
            assert ep.security is not None and ep.security != [], (
                f"[{golden.repo_id}] {expected.method} {expected.path} "
                f"should have auth, got security={ep.security}"
            )
        elif expected.has_auth is False:
            assert ep.security is None or ep.security == [], (
                f"[{golden.repo_id}] {expected.method} {expected.path} "
                f"should be public, got security={ep.security}"
            )

        # Check request body
        if expected.has_request_body is True:
            assert ep.request_body is not None, (
                f"[{golden.repo_id}] {expected.method} {expected.path} "
                f"should have request body"
            )
        elif expected.has_request_body is False:
            assert ep.request_body is None, (
                f"[{golden.repo_id}] {expected.method} {expected.path} "
                f"should NOT have request body"
            )

        # Check parameters
        if expected.param_names:
            actual_params = {p.name for p in ep.parameters}
            for param_name in expected.param_names:
                assert param_name in actual_params, (
                    f"[{golden.repo_id}] {expected.method} {expected.path} "
                    f"missing param '{param_name}', got: {actual_params}"
                )

        # Check minimum responses
        assert len(ep.responses) >= expected.min_responses, (
            f"[{golden.repo_id}] {expected.method} {expected.path} "
            f"expected at least {expected.min_responses} responses, "
            f"got {len(ep.responses)}"
        )


def assert_schemas_match(
    schemas: dict[str, dict],
    golden: SchemaLoopGolden,
) -> None:
    """Assert that extracted schemas match golden expectations."""
    resolved = {
        k: v for k, v in schemas.items() if not v.get("x-unresolved")
    }

    assert len(resolved) >= golden.min_schemas, (
        f"[{golden.repo_id}] Expected at least {golden.min_schemas} resolved schemas, "
        f"got {len(resolved)}: {list(resolved.keys())}"
    )

    for expected in golden.expected_schemas:
        assert expected.name in schemas, (
            f"[{golden.repo_id}] Expected schema '{expected.name}' not found. "
            f"Got: {list(schemas.keys())}"
        )

        schema = schemas[expected.name]

        # Skip detailed checks for unresolved schemas
        if schema.get("x-unresolved"):
            continue

        props = schema.get("properties", {})
        assert len(props) >= expected.min_properties, (
            f"[{golden.repo_id}] Schema '{expected.name}' expected at least "
            f"{expected.min_properties} properties, got {len(props)}: {list(props.keys())}"
        )

        for prop_name in expected.expected_properties:
            assert prop_name in props, (
                f"[{golden.repo_id}] Schema '{expected.name}' missing property "
                f"'{prop_name}', got: {list(props.keys())}"
            )

        if expected.expected_required:
            required = schema.get("required", [])
            for req_name in expected.expected_required:
                assert req_name in required, (
                    f"[{golden.repo_id}] Schema '{expected.name}' missing required "
                    f"field '{req_name}', got required: {required}"
                )


# --- Scout golden data ---


@dataclass
class ScoutGolden:
    """Golden data for a Scout agent test case."""

    repo_id: str
    repo_dir: str  # Relative to REPOS_ROOT
    framework: str  # Expected framework name (case-insensitive match)
    language: str  # Expected language name (case-insensitive match)
    route_files: list[str] = field(default_factory=list)  # Expected route files (relative to repo root)
    min_route_files: int = 1  # Minimum number of route files to find
    servers: list[str] = field(default_factory=list)  # Expected server URL substrings
    base_path: str | None = None  # Expected base_path, None=don't check


def assert_scout_match(
    manifest: "DiscoveryManifest",
    golden: ScoutGolden,
) -> None:
    """Assert that the Scout's discovery manifest matches golden expectations.

    Uses flexible matching:
    - Framework/language are case-insensitive substring matches
    - Route files check that expected files are found (allows extras)
    - Servers check that expected substrings appear in at least one server URL
    """
    from swagger_agent.models import DiscoveryManifest

    # Framework (case-insensitive substring)
    assert golden.framework.lower() in manifest.framework.lower(), (
        f"[{golden.repo_id}] Expected framework containing '{golden.framework}', "
        f"got '{manifest.framework}'"
    )

    # Language (case-insensitive substring)
    assert golden.language.lower() in manifest.language.lower(), (
        f"[{golden.repo_id}] Expected language containing '{golden.language}', "
        f"got '{manifest.language}'"
    )

    # Route files — minimum count
    assert len(manifest.route_files) >= golden.min_route_files, (
        f"[{golden.repo_id}] Expected at least {golden.min_route_files} route files, "
        f"got {len(manifest.route_files)}: {manifest.route_files}"
    )

    # Route files — each expected file must be found (normalize for comparison)
    manifest_files_normalized = [_normalize_route_path(f) for f in manifest.route_files]
    for expected_file in golden.route_files:
        expected_norm = _normalize_route_path(expected_file)
        found = any(expected_norm in mf or mf.endswith(expected_norm) for mf in manifest_files_normalized)
        assert found, (
            f"[{golden.repo_id}] Expected route file '{expected_file}' not found in "
            f"manifest. Got: {manifest.route_files}"
        )

    # Servers — each expected substring must appear in at least one server URL
    for expected_url in golden.servers:
        found = any(expected_url in s for s in manifest.servers)
        assert found, (
            f"[{golden.repo_id}] Expected server URL containing '{expected_url}' "
            f"not found. Got: {manifest.servers}"
        )

    # Base path
    if golden.base_path is not None:
        assert manifest.base_path == golden.base_path, (
            f"[{golden.repo_id}] Expected base_path='{golden.base_path}', "
            f"got '{manifest.base_path}'"
        )


def _normalize_route_path(path: str) -> str:
    """Normalize a route file path for comparison.

    Strips leading ./ and trailing whitespace, normalizes separators.
    Handles both absolute and relative paths by extracting the relative portion.
    """
    import os
    path = path.strip().replace("\\", "/")
    # Strip leading ./
    while path.startswith("./"):
        path = path[2:]
    # If absolute, try to extract from repo root onwards
    # (Scout may return absolute paths)
    return path


# --- Pipeline golden data ---


@dataclass
class ExpectedPipelineEndpoint:
    """Golden expectation for an endpoint in the assembled spec."""

    method: str
    path: str  # OpenAPI-style path with {param}
    has_auth: bool | None = None
    has_request_body: bool | None = None
    param_names: list[str] = field(default_factory=list)
    min_responses: int = 1
    response_schema_ref: str | None = None  # Expected $ref schema name (e.g. "Project")


@dataclass
class PipelineGolden:
    """Golden data for a full pipeline test case."""

    repo_id: str
    repo_dir: str
    min_endpoints: int
    min_schemas: int
    endpoints: list[ExpectedPipelineEndpoint] = field(default_factory=list)
    expected_schemas: list[ExpectedSchema] = field(default_factory=list)
    expected_security_schemes: list[str] = field(default_factory=list)
    expected_servers: list[str] = field(default_factory=list)
    # Completeness checks that must be True
    completeness_must_pass: list[str] = field(default_factory=list)
    max_validation_errors: int = 0
    max_unresolved_schemas: int = 0


def assert_pipeline_match(
    spec: dict,
    schemas: dict[str, dict],
    golden: PipelineGolden,
    validation_errors: list[str] | None = None,
) -> None:
    """Assert that the assembled spec matches pipeline golden expectations."""
    paths = spec.get("paths", {})
    spec_schemas = spec.get("components", {}).get("schemas", {})
    security_schemes = spec.get("components", {}).get("securitySchemes", {})
    servers = spec.get("servers", [])

    # Total endpoint count
    total_ops = sum(
        1 for methods in paths.values()
        for m, op in methods.items()
        if isinstance(op, dict)
    )
    assert total_ops >= golden.min_endpoints, (
        f"[{golden.repo_id}] Expected at least {golden.min_endpoints} endpoints, "
        f"got {total_ops}"
    )

    # Schema count
    resolved_schemas = {
        k: v for k, v in spec_schemas.items() if not v.get("x-unresolved")
    }
    assert len(resolved_schemas) >= golden.min_schemas, (
        f"[{golden.repo_id}] Expected at least {golden.min_schemas} resolved schemas, "
        f"got {len(resolved_schemas)}: {list(resolved_schemas.keys())}"
    )

    # Unresolved schema count
    unresolved_count = sum(1 for v in spec_schemas.values() if v.get("x-unresolved"))
    assert unresolved_count <= golden.max_unresolved_schemas, (
        f"[{golden.repo_id}] Expected at most {golden.max_unresolved_schemas} unresolved schemas, "
        f"got {unresolved_count}: "
        f"{[k for k, v in spec_schemas.items() if v.get('x-unresolved')]}"
    )

    # Check specific endpoints
    for expected in golden.endpoints:
        # Normalize expected path
        norm_expected = normalize_path(expected.path)

        # Find matching operation in spec
        found_op = None
        for path_key, methods in paths.items():
            norm_path = normalize_path(path_key)
            if norm_path == norm_expected:
                method_key = expected.method.lower()
                if method_key in methods:
                    found_op = methods[method_key]
                    break

        assert found_op is not None, (
            f"[{golden.repo_id}] Expected endpoint {expected.method} {expected.path} "
            f"not found in spec. Got paths: {list(paths.keys())}"
        )

        # Check auth
        if expected.has_auth is True:
            sec = found_op.get("security")
            assert sec is not None and sec != [], (
                f"[{golden.repo_id}] {expected.method} {expected.path} "
                f"should have auth, got security={sec}"
            )
        elif expected.has_auth is False:
            sec = found_op.get("security")
            assert sec is not None and (sec == [] or all(not s for s in sec)), (
                f"[{golden.repo_id}] {expected.method} {expected.path} "
                f"should be explicitly public (security: []), got security={sec}"
            )

        # Check request body
        if expected.has_request_body is True:
            assert "requestBody" in found_op, (
                f"[{golden.repo_id}] {expected.method} {expected.path} "
                f"should have requestBody"
            )

        # Check response schema ref
        if expected.response_schema_ref:
            resp_200 = found_op.get("responses", {}).get("200", {})
            content = resp_200.get("content", {})
            ref_found = False
            for _ct, media in content.items():
                schema = media.get("schema", {})
                ref_val = schema.get("$ref", "")
                items_ref = schema.get("items", {}).get("$ref", "") if schema.get("type") == "array" else ""
                if expected.response_schema_ref in ref_val or expected.response_schema_ref in items_ref:
                    ref_found = True
            assert ref_found, (
                f"[{golden.repo_id}] {expected.method} {expected.path} "
                f"expected response $ref containing '{expected.response_schema_ref}', "
                f"got responses: {found_op.get('responses', {}).get('200', {})}"
            )

    # Check expected schemas
    for expected_schema in golden.expected_schemas:
        assert expected_schema.name in spec_schemas, (
            f"[{golden.repo_id}] Expected schema '{expected_schema.name}' not in spec. "
            f"Got: {list(spec_schemas.keys())}"
        )
        schema = spec_schemas[expected_schema.name]
        if not schema.get("x-unresolved"):
            props = schema.get("properties", {})
            for prop_name in expected_schema.expected_properties:
                assert prop_name in props, (
                    f"[{golden.repo_id}] Schema '{expected_schema.name}' missing "
                    f"property '{prop_name}', got: {list(props.keys())}"
                )

    # Check security schemes
    for scheme_name in golden.expected_security_schemes:
        assert scheme_name in security_schemes, (
            f"[{golden.repo_id}] Expected security scheme '{scheme_name}' not found. "
            f"Got: {list(security_schemes.keys())}"
        )

    # Check servers
    server_urls = [s.get("url", "") for s in servers]
    for expected_url in golden.expected_servers:
        assert any(expected_url in url for url in server_urls), (
            f"[{golden.repo_id}] Expected server URL containing '{expected_url}' "
            f"not found. Got: {server_urls}"
        )

    # Check $ref validity: no double-nested refs
    import json
    spec_json = json.dumps(spec)
    assert "#/components/schemas/#" not in spec_json, (
        f"[{golden.repo_id}] Double-nested $ref found in spec (e.g. "
        f"'#/components/schemas/#/components/schemas/Foo')"
    )

    # Check path param format: no Express-style :param in paths
    for path_key in paths.keys():
        assert ":" not in path_key, (
            f"[{golden.repo_id}] Path '{path_key}' uses Express-style :param "
            f"instead of OpenAPI {{param}} format"
        )

    # Check validation errors
    if validation_errors is not None:
        assert len(validation_errors) <= golden.max_validation_errors, (
            f"[{golden.repo_id}] Expected at most {golden.max_validation_errors} "
            f"validation errors, got {len(validation_errors)}: {validation_errors}"
        )
