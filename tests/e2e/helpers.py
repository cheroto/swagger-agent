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
