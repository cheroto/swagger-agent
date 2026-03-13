"""E2E tests for known issues — runs actual LLM against real repos.

Each test targets a specific issue category discovered during e2e analysis.
Tests assert the DESIRED behavior — they FAIL when the issue persists.

Run: pytest tests/e2e/test_known_issues.py -m e2e -v
"""

from __future__ import annotations

import os

import pytest

from swagger_agent.agents.route_extractor.harness import (
    RouteExtractorContext,
    run_route_extractor,
)
from swagger_agent.config import LLMConfig
from swagger_agent.infra.assembler import assemble_spec
from swagger_agent.infra.validator import check_completeness, validate_spec
from swagger_agent.models import DiscoveryManifest
from swagger_agent.pipeline import run_pipeline

from .conftest import e2e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_route_extractor_for(
    repos_root: str,
    repo_dir: str,
    route_file: str,
    framework: str,
    base_path: str,
    llm_config: LLMConfig,
):
    """Run the route extractor on a specific file and return (descriptor, record)."""
    repo_path = os.path.join(repos_root, repo_dir)
    abs_route = os.path.join(repo_path, route_file)
    if not os.path.isfile(abs_route):
        pytest.skip(f"Route file not found: {abs_route}")

    context = RouteExtractorContext(
        framework=framework,
        base_path=base_path,
        target_file=abs_route,
    )
    return run_route_extractor(
        target_file=abs_route,
        context=context,
        config=llm_config,
    )


# ---------------------------------------------------------------------------
# Issue 1: Ctags prefilter must not strip expression-bodied C# methods
#
# The prefilter should fall back to the full file when ctags can't parse
# methods. The LLM must still extract all endpoints.
# ---------------------------------------------------------------------------


@e2e
class TestCtagsPrefilterExpressionBodied:
    """Expression-bodied C# controllers must produce endpoints, not 0."""

    def test_aspnetcore_articles_controller_extracts_endpoints(
        self, repos_root: str, llm_config: LLMConfig
    ):
        """ArticlesController.cs uses => syntax. Must extract 6 endpoints."""
        descriptor, record = _run_route_extractor_for(
            repos_root,
            "aspnetcore-realworld",
            "src/Conduit/Features/Articles/ArticlesController.cs",
            framework="aspnetcore",
            base_path="",
            llm_config=llm_config,
        )
        assert len(descriptor.endpoints) >= 6, (
            f"Expected >= 6 endpoints from ArticlesController (expression-bodied C#), "
            f"got {len(descriptor.endpoints)}: "
            f"{[(e.method, e.path) for e in descriptor.endpoints]}"
        )

    def test_aspnetcore_comments_controller_extracts_endpoints(
        self, repos_root: str, llm_config: LLMConfig
    ):
        """CommentsController.cs uses => syntax. Must extract 3 endpoints."""
        descriptor, record = _run_route_extractor_for(
            repos_root,
            "aspnetcore-realworld",
            "src/Conduit/Features/Comments/CommentsController.cs",
            framework="aspnetcore",
            base_path="",
            llm_config=llm_config,
        )
        assert len(descriptor.endpoints) >= 3, (
            f"Expected >= 3 endpoints from CommentsController, "
            f"got {len(descriptor.endpoints)}"
        )


# ---------------------------------------------------------------------------
# Issue 2: LLM must produce valid import_source on RefHint (not garbage)
#
# The LLM must provide a usable namespace/import for class_to_file refs.
# Echoing the resolution value or providing bare dotted strings without
# a keyword prefix is not acceptable.
# ---------------------------------------------------------------------------


@e2e
class TestRefHintImportSource:
    """Route extractor must produce usable import_source on RefHints."""

    def test_class_to_file_refs_have_file_namespace(
        self, repos_root: str, llm_config: LLMConfig
    ):
        """ArticlesController references Create.Command and Edit.Model.
        file_namespace must contain the namespace of the current file."""
        descriptor, _ = _run_route_extractor_for(
            repos_root,
            "aspnetcore-realworld",
            "src/Conduit/Features/Articles/ArticlesController.cs",
            framework="aspnetcore",
            base_path="",
            llm_config=llm_config,
        )

        for ep in descriptor.endpoints:
            refs = []
            if ep.request_body:
                refs.append(("request_body", ep.request_body.schema_ref))
            for resp in ep.responses:
                if resp.schema_ref:
                    refs.append((f"response_{resp.status_code}", resp.schema_ref))

            for location, ref in refs:
                if ref.resolution == "class_to_file":
                    # file_namespace must be set and useful
                    assert ref.file_namespace and len(ref.file_namespace) > 5, (
                        f"[{ep.method} {ep.path} {location}] "
                        f"file_namespace is '{ref.file_namespace}' — "
                        f"must contain the namespace/package of the current file "
                        f"for disambiguation."
                    )
                    # file_namespace must NOT be the resolution value echoed back
                    assert ref.file_namespace not in ("class_to_file", "import", "unresolvable"), (
                        f"[{ep.method} {ep.path} {location}] "
                        f"file_namespace is '{ref.file_namespace}' — "
                        f"LLM echoed a resolution value instead of the actual namespace."
                    )

    def test_import_refs_have_actual_import_lines(
        self, repos_root: str, llm_config: LLMConfig
    ):
        """FavoritesController imports from Articles namespace.
        import_line must be the actual using statement."""
        descriptor, _ = _run_route_extractor_for(
            repos_root,
            "aspnetcore-realworld",
            "src/Conduit/Features/Favorites/FavoritesController.cs",
            framework="aspnetcore",
            base_path="",
            llm_config=llm_config,
        )

        for ep in descriptor.endpoints:
            for resp in ep.responses:
                if resp.schema_ref and resp.schema_ref.resolution == "import":
                    assert resp.schema_ref.import_line, (
                        f"[{ep.method} {ep.path}] import_line must be set for "
                        f"resolution='import'"
                    )
                    assert "using" in resp.schema_ref.import_line or \
                           "import" in resp.schema_ref.import_line or \
                           "from" in resp.schema_ref.import_line, (
                        f"[{ep.method} {ep.path}] import_line should contain an "
                        f"import keyword, got: '{resp.schema_ref.import_line}'"
                    )


# ---------------------------------------------------------------------------
# Issue 3: Security must be explicitly set on every endpoint
#
# The LLM must set security: [] for public endpoints and
# security: ["BearerAuth"] for protected ones. Never omit.
# ---------------------------------------------------------------------------


@e2e
class TestSecurityAlwaysExplicit:
    """Every endpoint must have explicit security declaration."""

    def test_aspnetcore_mixed_auth_endpoints(
        self, repos_root: str, llm_config: LLMConfig
    ):
        """ArticlesController has both public GETs and protected POST/PUT/DELETE.
        Every endpoint must have security set ([] or ["BearerAuth"])."""
        descriptor, _ = _run_route_extractor_for(
            repos_root,
            "aspnetcore-realworld",
            "src/Conduit/Features/Articles/ArticlesController.cs",
            framework="aspnetcore",
            base_path="",
            llm_config=llm_config,
        )

        for ep in descriptor.endpoints:
            # security is now a required list (never None)
            assert ep.security is not None, (
                f"[{ep.method} {ep.path}] security is None — "
                f"must be [] (public) or ['BearerAuth'] (protected)"
            )
            assert isinstance(ep.security, list), (
                f"[{ep.method} {ep.path}] security must be a list"
            )

        # Specifically: POST/PUT/DELETE should have auth
        write_endpoints = [
            ep for ep in descriptor.endpoints
            if ep.method.upper() in ("POST", "PUT", "DELETE")
        ]
        for ep in write_endpoints:
            assert len(ep.security) > 0, (
                f"[{ep.method} {ep.path}] write endpoint should have auth, "
                f"got security={ep.security}"
            )


# ---------------------------------------------------------------------------
# Issue 4: Assembled spec must have no empty required arrays
#
# The assembler must strip required: [] from schemas. This tests the
# full pipeline including LLM-produced schemas.
# ---------------------------------------------------------------------------


@e2e
class TestNoEmptyRequiredArrays:
    """Assembled spec must not contain required: [] in any schema."""

    def test_spring_boot_schemas_no_empty_required(
        self, repos_root: str, llm_config: LLMConfig
    ):
        """spring-boot-blog previously produced Address with required: [].
        After the assembler fix, this should not happen."""
        repo_path = os.path.join(repos_root, "spring-boot-blog")
        if not os.path.isdir(repo_path):
            pytest.skip(f"Repo not found: {repo_path}")

        result = run_pipeline(target_dir=repo_path, config=llm_config)

        schemas = result.spec.get("components", {}).get("schemas", {})
        for name, schema in schemas.items():
            req = schema.get("required")
            if req is not None:
                assert len(req) > 0, (
                    f"Schema '{name}' has required: [] — "
                    f"assembler should strip empty required arrays"
                )

        # The spec should also pass validation (no structural errors)
        assert len(result.validation.errors) == 0, (
            f"Expected 0 validation errors, got {len(result.validation.errors)}: "
            f"{result.validation.errors[0][:100] if result.validation.errors else ''}"
        )


# ---------------------------------------------------------------------------
# Issue 5: Full pipeline for aspnetcore-realworld must produce good output
#
# End-to-end: all 8 controllers found, endpoints extracted, schemas resolved,
# no validation errors.
# ---------------------------------------------------------------------------


@e2e
class TestAspnetcorePipelineQuality:
    """Full pipeline on aspnetcore-realworld must meet quality bar."""

    def test_pipeline_endpoint_count(
        self, repos_root: str, llm_config: LLMConfig
    ):
        """Must extract at least 15 endpoints across 8 controllers."""
        repo_path = os.path.join(repos_root, "aspnetcore-realworld")
        if not os.path.isdir(repo_path):
            pytest.skip(f"Repo not found: {repo_path}")

        result = run_pipeline(target_dir=repo_path, config=llm_config)

        # Count total endpoints in spec
        total = sum(
            1 for path, methods in result.spec.get("paths", {}).items()
            for method, op in methods.items()
            if isinstance(op, dict)
        )
        assert total >= 15, (
            f"Expected >= 15 endpoints from aspnetcore-realworld (8 controllers), "
            f"got {total}"
        )

        # Must have schemas
        schemas = result.spec.get("components", {}).get("schemas", {})
        resolved = {n: s for n, s in schemas.items() if not s.get("x-unresolved")}
        assert len(resolved) >= 8, (
            f"Expected >= 8 resolved schemas, got {len(resolved)}: "
            f"{list(resolved.keys())}"
        )

        # Unresolved should be few
        unresolved = {n for n, s in schemas.items() if s.get("x-unresolved")}
        assert len(unresolved) <= 3, (
            f"Expected <= 3 unresolved schemas, got {len(unresolved)}: {unresolved}"
        )

        # No validation errors
        assert len(result.validation.errors) == 0, (
            f"Expected 0 validation errors: {result.validation.errors}"
        )

        # Key completeness checks
        assert result.completeness.has_endpoints is True
        assert result.completeness.has_security_schemes is True
        assert result.completeness.endpoints_have_auth is True
        assert result.completeness.has_schemas is True
        assert result.completeness.has_servers is True
