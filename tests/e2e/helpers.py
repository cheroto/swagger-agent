"""Assertion helpers for LLM-based agent e2e tests.

Since LLM output varies between runs, these helpers use structural assertions
(correct endpoints found, correct properties present) rather than exact JSON match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from swagger_agent.models import Endpoint, EndpointDescriptor


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
class RouteGolden:
    """Golden data for a route extraction test case."""

    repo_id: str
    repo_dir: str  # Relative to REPOS_ROOT
    route_file: str  # Relative to repo_dir
    framework: str
    base_path: str
    min_endpoints: int
    endpoints: list[ExpectedEndpoint]


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
        # Escape special chars but keep {param} as wildcard
        pattern = re.escape(expected.path)
        pattern = re.sub(r"\\{\\w+\\}", r"\\{\\w+\\}", pattern)
        # Also handle cases where LLM might use different param names
        pattern = re.sub(r"\\{[^}]+\\}", r"[^/]+", pattern)

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
