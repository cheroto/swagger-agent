"""Tests for known issues found during e2e analysis.

Each test targets one category of issue discovered across multiple repos.
All tests assert the DESIRED/CORRECT behavior — they were originally written
to FAIL, confirming bugs. As fixes are applied, these tests should PASS.

All tests use synthetic/simulated data — no LLM calls needed.

Run: pytest tests/test_known_issues.py -v
"""

from __future__ import annotations

import pytest

from swagger_agent.infra.assembler import assemble_spec
from swagger_agent.infra.validator import check_completeness, validate_spec
from swagger_agent.models import (
    DiscoveryManifest,
    Endpoint,
    EndpointDescriptor,
    Parameter,
    RefHint,
    RequestBody,
    Response,
    SecurityRequirement,
)


def _make_manifest(**overrides) -> DiscoveryManifest:
    defaults = dict(
        framework="test",
        language="test",
        route_files=["routes.py"],
        servers=["http://localhost:8080"],
        base_path="",
    )
    defaults.update(overrides)
    return DiscoveryManifest(**defaults)


def _make_descriptor(endpoints: list[Endpoint], source_file: str = "routes.py") -> EndpointDescriptor:
    return EndpointDescriptor(source_file=source_file, endpoints=endpoints)


def _unresolvable_ref(name: str = "object") -> RefHint:
    """Helper to create an unresolvable RefHint."""
    return RefHint(ref_hint=name, import_line="", file_namespace="", resolution="unresolvable")


# ---------------------------------------------------------------------------
# Issue 1: Ctags prefilter strips expression-bodied C# methods
#
# Observed in: aspnetcore-realworld (7/8 controllers → 0 endpoints)
# C# expression-bodied methods (=>) are invisible to ctags. The prefilter
# strips the entire class body because it finds no method tags, leaving
# just imports + class declaration. The LLM sees an empty class and
# extracts 0 endpoints.
#
# Fix: Detect lost decorator/annotation lines after filtering. If any @,
# [, or #[ lines from the original body are missing in filtered output,
# fall back to the full file.
# ---------------------------------------------------------------------------


class TestCtagsPrefilterExpressionBodiedMethods:
    """Prefilter must preserve route attributes for expression-bodied C# methods."""

    def test_prefilter_preserves_http_attributes(self):
        """Filtered content must contain [HttpGet], [HttpPost] etc. attributes
        even when methods use expression-body syntax (=>)."""
        import shutil
        import tempfile
        from pathlib import Path

        from swagger_agent.infra.ctags_filter import prefilter_route_file

        if not shutil.which("ctags"):
            pytest.skip("universal-ctags not installed")

        controller_code = '''using System.Threading;
using System.Threading.Tasks;
using MediatR;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace Conduit.Features.Articles;

[Route("articles")]
public class ArticlesController(IMediator mediator) : Controller
{
    [HttpGet]
    public Task<ArticlesEnvelope> Get(
        [FromQuery] string tag,
        [FromQuery] string author,
        CancellationToken cancellationToken
    ) => mediator.Send(new List.Query(tag, author), cancellationToken);

    [HttpGet("{slug}")]
    public Task<ArticleEnvelope> Get(string slug, CancellationToken cancellationToken) =>
        mediator.Send(new Details.Query(slug), cancellationToken);

    [HttpPost]
    [Authorize(AuthenticationSchemes = "Bearer")]
    public Task<ArticleEnvelope> Create(
        [FromBody] CreateCommand command,
        CancellationToken cancellationToken
    ) => mediator.Send(command, cancellationToken);

    [HttpPut("{slug}")]
    [Authorize(AuthenticationSchemes = "Bearer")]
    public Task<ArticleEnvelope> Edit(
        string slug,
        [FromBody] EditModel model,
        CancellationToken cancellationToken
    ) => mediator.Send(new EditCommand(model, slug), cancellationToken);

    [HttpDelete("{slug}")]
    [Authorize(AuthenticationSchemes = "Bearer")]
    public Task Delete(string slug, CancellationToken cancellationToken) =>
        mediator.Send(new DeleteCommand(slug), cancellationToken);
}
'''
        with tempfile.NamedTemporaryFile(
            suffix=".cs", mode="w", delete=False
        ) as f:
            f.write(controller_code)
            tmp_path = f.name

        try:
            handler_names = ["Get", "GetFeed", "Create", "Edit", "Delete"]
            result = prefilter_route_file(tmp_path, controller_code, handler_names)

            # DESIRED: The filtered content must preserve HTTP route attributes
            # and method signatures so the LLM can extract endpoints.
            assert "[HttpGet]" in result.content, (
                "Filtered content must contain [HttpGet] attribute. "
                "The prefilter stripped expression-bodied methods because ctags "
                "can't parse them (=> syntax). Content was reduced to just "
                "imports + empty class shell."
            )
            assert "[HttpPost]" in result.content, (
                "Filtered content must contain [HttpPost] attribute"
            )
            assert "[Authorize" in result.content, (
                "Filtered content must contain [Authorize] attribute"
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Issue 2: Assembler passes through "required": [] which violates OpenAPI
#
# Observed in: rest-api-node (Project), spring-boot-blog (Address)
# The LLM outputs schemas with "required": [] (empty array). OpenAPI 3.0
# says: if "required" is present, it MUST be non-empty. The assembler
# should strip empty required arrays before emitting the spec.
#
# Fix: Add _strip_empty_required() post-processing pass in assembler.
# ---------------------------------------------------------------------------


class TestEmptyRequiredArrayStripped:
    """Assembler must strip empty required arrays from schemas."""

    def test_assembler_strips_empty_required_array(self):
        """Schemas with required: [] should have the key removed by the
        assembler so the spec passes OpenAPI validation."""
        manifest = _make_manifest()
        descriptor = _make_descriptor([
            Endpoint(
                method="GET",
                path="/items",
                operation_id="getItems",
                responses=[Response(
                    status_code="200",
                    description="OK",
                    schema_ref=RefHint(ref_hint="Item", import_line="", file_namespace="", resolution="unresolvable"),
                )],
            ),
        ])

        schemas = {
            "Item": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                },
                "required": [],  # OpenAPI 3.0 violation — must be non-empty if present
            },
        }

        result = assemble_spec(manifest, [descriptor], schemas)
        spec_schemas = result.spec.get("components", {}).get("schemas", {})

        # DESIRED: The assembler should strip empty required arrays
        item_schema = spec_schemas.get("Item", {})
        assert item_schema.get("required") is None or item_schema.get("required") != [], (
            "Assembler must strip 'required': [] from schemas. "
            "Empty required arrays violate OpenAPI 3.0 spec."
        )

        # DESIRED: The assembled spec should pass validation
        validation = validate_spec(result.spec)
        assert len(validation.errors) == 0, (
            f"Spec should have 0 validation errors after stripping required: []. "
            f"Got {len(validation.errors)}: {validation.errors[0][:100] if validation.errors else ''}"
        )


# ---------------------------------------------------------------------------
# Issue 3: has_request_bodies false-positives on bodyless PUT/PATCH
#
# Observed in: spring-boot-blog (PUT /completeTodo, PUT /giveAdmin)
# The checker flags ALL PUT/PATCH without requestBody. But state-toggle
# endpoints (PUT /complete, PUT /giveAdmin) legitimately have no body.
#
# Fix: Use descriptors as ground truth. Only flag endpoints where the
# route extractor explicitly produced a request_body.
# ---------------------------------------------------------------------------


class TestRequestBodyFalsePositive:
    """has_request_bodies must not flag legitimate bodyless PUT endpoints."""

    def test_bodyless_put_toggle_passes_completeness(self):
        """PUT endpoints that toggle state (no body needed) should not
        cause has_request_bodies to be False."""
        manifest = _make_manifest()
        descriptors = [
            _make_descriptor([
                Endpoint(
                    method="PUT",
                    path="/items/{id}",
                    operation_id="updateItem",
                    security=[SecurityRequirement(name="BearerAuth", scheme_type="bearer")],
                    parameters=[Parameter(name="id", **{"in": "path"}, required=True)],
                    request_body=RequestBody(
                        content_type="application/json",
                        schema_ref=_unresolvable_ref("UpdateItemRequest"),
                    ),
                    responses=[Response(status_code="200", description="OK", schema_ref=_unresolvable_ref())],
                ),
                # State toggle — legitimately no body
                Endpoint(
                    method="PUT",
                    path="/items/{id}/complete",
                    operation_id="completeItem",
                    security=[SecurityRequirement(name="BearerAuth", scheme_type="bearer")],
                    parameters=[Parameter(name="id", **{"in": "path"}, required=True)],
                    responses=[Response(status_code="200", description="OK", schema_ref=_unresolvable_ref())],
                ),
            ]),
        ]

        result = assemble_spec(manifest, descriptors, {})
        completeness = check_completeness(result.spec, manifest, descriptors)

        # DESIRED: has_request_bodies should be True — the bodyless PUT is
        # a legitimate state toggle, not a missing body.
        assert completeness.has_request_bodies is True, (
            "has_request_bodies should be True when bodyless PUT endpoints are "
            "state toggles. The checker currently flags ALL PUT without body."
        )


# ---------------------------------------------------------------------------
# Issue 4: No warning when POST endpoints have opaque request bodies
#
# Observed in: laravel-realworld (0 schemas), levo-schema-service (0 schemas)
# When the route extractor produces requestBody with an unresolvable
# schema_ref, the assembler emits bare type: object. The validator should
# warn about these opaque bodies.
#
# Fix: RequestBody.schema_ref is now required (not Optional). The LLM must
# always provide a ref_hint. The validator also warns when the assembled
# spec has bare type: object with no properties.
# ---------------------------------------------------------------------------


class TestOpaqueBodyWarning:
    """Validator should warn about opaque request body schemas."""

    def test_bare_object_body_produces_warning(self):
        """POST endpoints with requestBody whose schema is bare type: object
        (no properties, no $ref) should produce a validation warning."""
        manifest = _make_manifest()
        descriptors = [
            _make_descriptor([
                Endpoint(
                    method="POST",
                    path="/users/login",
                    operation_id="login",
                    request_body=RequestBody(
                        content_type="application/json",
                        schema_ref=_unresolvable_ref("LoginRequest"),
                    ),
                    responses=[Response(status_code="200", description="OK", schema_ref=_unresolvable_ref())],
                ),
                Endpoint(
                    method="POST",
                    path="/articles",
                    operation_id="createArticle",
                    security=[SecurityRequirement(name="BearerAuth", scheme_type="bearer")],
                    request_body=RequestBody(
                        content_type="application/json",
                        schema_ref=_unresolvable_ref("CreateArticleRequest"),
                    ),
                    responses=[Response(status_code="201", description="Created", schema_ref=_unresolvable_ref())],
                ),
            ]),
        ]

        # Pass empty schemas — unresolvable refs produce placeholder schemas
        result = assemble_spec(manifest, descriptors, {})
        validation = validate_spec(result.spec)

        # DESIRED: The validator should warn about unresolved/opaque schemas.
        # With schema_ref now required, unresolvable refs create x-unresolved
        # placeholder schemas. The validator should flag these.
        relevant_warnings = [
            w for w in validation.warnings
            if "unresolved" in w.lower() or "opaque" in w.lower()
        ]
        assert len(relevant_warnings) > 0, (
            "Validator should warn about unresolved or opaque request body schemas. "
            f"Got warnings: {validation.warnings}"
        )

    def test_schema_ref_is_required_on_request_body(self):
        """RequestBody.schema_ref must be required (not Optional).
        Creating a RequestBody without schema_ref should fail validation."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RequestBody(content_type="application/json")  # Missing schema_ref


# ---------------------------------------------------------------------------
# Issue 5: Assembler doesn't deduplicate path variants
#
# Observed in: passwordless-auth-rust (30 endpoints for 9-endpoint API)
# When multiple route files produce endpoints for the same logical route
# with different path formats (/request/magic vs /request-magic), the
# assembler creates separate path entries with no warning.
#
# Fix: Add _detect_duplicate_paths() to the validator that normalizes
# separators and warns about potential duplicates.
# ---------------------------------------------------------------------------


class TestEndpointDuplicationDetection:
    """Validator should detect duplicate endpoints from overlapping files."""

    def test_duplicate_paths_produce_warning(self):
        """When two descriptors produce POST endpoints with paths that look
        like variants of the same route, the validator should warn."""
        manifest = _make_manifest(route_files=["routes.rs", "handlers.rs"])

        desc_routes = _make_descriptor(
            [
                Endpoint(
                    method="POST",
                    path="/request/magic",
                    operation_id="request_magic",
                    responses=[Response(status_code="200", description="OK", schema_ref=_unresolvable_ref())],
                ),
                Endpoint(
                    method="POST",
                    path="/totp/enroll",
                    operation_id="totp_enroll",
                    responses=[Response(status_code="200", description="OK", schema_ref=_unresolvable_ref())],
                ),
            ],
            source_file="routes.rs",
        )

        desc_handlers = _make_descriptor(
            [
                Endpoint(
                    method="POST",
                    path="/request-magic",
                    operation_id="request_magic_handler",
                    responses=[Response(status_code="200", description="OK", schema_ref=_unresolvable_ref())],
                ),
                Endpoint(
                    method="POST",
                    path="/totp-enroll",
                    operation_id="totp_enroll_handler",
                    responses=[Response(status_code="200", description="OK", schema_ref=_unresolvable_ref())],
                ),
            ],
            source_file="handlers.rs",
        )

        result = assemble_spec(manifest, [desc_routes, desc_handlers], {})
        validation = validate_spec(result.spec)

        # DESIRED: The validator should detect that /request/magic and
        # /request-magic are likely the same endpoint and warn about it.
        duplicate_warnings = [
            w for w in validation.warnings
            if "duplicate" in w.lower()
        ]
        assert len(duplicate_warnings) > 0, (
            f"Validator should warn about potential duplicate paths "
            f"(/request/magic vs /request-magic). "
            f"Warnings: {validation.warnings}"
        )


# ---------------------------------------------------------------------------
# Issue 6: endpoints_have_auth fails when public endpoints omit security key
#
# Observed in: rest-api-node, laravel-realworld, passwordless-auth-rust
#
# Fix: Endpoint.security is now list[str] (non-optional, defaults to []).
# The assembler always emits the security key. The completeness checker's
# all("security" in op) check now works correctly because every operation
# has the key.
# ---------------------------------------------------------------------------


class TestEndpointsHaveAuthPublicOmission:
    """endpoints_have_auth must pass when public endpoints use security=[]."""

    def test_mixed_auth_and_public_endpoints_pass(self):
        """A spec with protected endpoints (security: [...]) and public
        endpoints (security: []) should pass endpoints_have_auth."""
        manifest = _make_manifest()
        descriptors = [
            _make_descriptor([
                Endpoint(
                    method="POST",
                    path="/articles",
                    operation_id="createArticle",
                    security=[SecurityRequirement(name="BearerAuth", scheme_type="bearer")],
                    request_body=RequestBody(
                        content_type="application/json",
                        schema_ref=_unresolvable_ref("CreateArticleRequest"),
                    ),
                    responses=[Response(status_code="200", description="OK", schema_ref=_unresolvable_ref())],
                ),
                Endpoint(
                    method="GET",
                    path="/articles",
                    operation_id="listArticles",
                    security=[],  # Explicitly public
                    responses=[Response(status_code="200", description="OK", schema_ref=_unresolvable_ref())],
                ),
            ]),
        ]

        result = assemble_spec(manifest, descriptors, {})
        spec = result.spec

        # Verify both operations have the "security" key in the assembled spec
        post_op = spec["paths"]["/articles"]["post"]
        get_op = spec["paths"]["/articles"]["get"]
        assert "security" in post_op, "POST should have security key"
        assert "security" in get_op, "GET should have security key (empty list = public)"

        completeness = check_completeness(spec, manifest, descriptors)

        # DESIRED: endpoints_have_auth should be True — every endpoint has
        # an explicit security declaration.
        assert completeness.endpoints_have_auth is True, (
            "endpoints_have_auth should be True when all endpoints have explicit "
            "security declarations (protected or public)."
        )

    def test_security_field_is_not_optional(self):
        """Endpoint.security should default to [] (not None)."""
        ep = Endpoint(
            method="GET",
            path="/test",
            operation_id="test",
            responses=[Response(status_code="200", description="OK", schema_ref=_unresolvable_ref())],
        )
        assert ep.security == [], (
            "Endpoint.security should default to [] (public), not None"
        )
        assert ep.security is not None, (
            "Endpoint.security must never be None"
        )
