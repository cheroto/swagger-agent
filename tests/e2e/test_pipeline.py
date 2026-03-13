"""E2E tests for the full pipeline (Scout -> Route Extraction -> Schema Loop -> Assembly -> Validation).

Tests the complete pipeline against test repos with golden data covering
endpoints, schemas, security schemes, and spec validity.

Run: pytest tests/e2e/test_pipeline.py -m e2e -v

Requires: universal-ctags installed, LLM server running, test repos available.
"""

from __future__ import annotations

import os

import pytest

from swagger_agent.config import LLMConfig
from swagger_agent.pipeline import run_pipeline

from .conftest import e2e
from .helpers import (
    ExpectedPipelineEndpoint,
    ExpectedSchema,
    PipelineGolden,
    assert_pipeline_match,
)

# ---------------------------------------------------------------------------
# Golden data — manually curated from reading each repo's source code
# ---------------------------------------------------------------------------

PIPELINE_GOLDEN: list[PipelineGolden] = [
    # -----------------------------------------------------------------------
    # 1. rest-api-node — Express.js + Mongoose + consign
    #
    # 5 route files:
    #   - src/routes/private/user.js:    PUT /users/update/:id, DELETE /users/delete/:id  (JWT)
    #   - src/routes/private/project.js: POST /projects/create, PUT /projects/update/:id,
    #                                    DELETE /projects/delete/:id  (JWT)
    #   - src/routes/public/user.js:     POST /users/create, GET /users, GET /users/select/:id
    #   - src/routes/public/project.js:  GET /projects, GET /projects/select/:id
    #   - src/routes/public/service.js:  GET /liveness_check, GET /readiness_check
    #
    # 2 Mongoose models: User (5 fields), Project (8 fields)
    # Auth: Bearer JWT on private routes
    # Server: http://localhost:8080
    # -----------------------------------------------------------------------
    PipelineGolden(
        repo_id="rest-api-node",
        repo_dir="rest-api-node",
        min_endpoints=10,  # 12 total, but allow some LLM variance
        min_schemas=2,  # User and Project
        endpoints=[
            # Private user routes (JWT required)
            ExpectedPipelineEndpoint(
                method="PUT",
                path="/users/update/{id}",
                has_auth=True,
                has_request_body=True,
                param_names=["id"],
                min_responses=2,
            ),
            ExpectedPipelineEndpoint(
                method="DELETE",
                path="/users/delete/{id}",
                has_auth=True,
                has_request_body=False,
                param_names=["id"],
                min_responses=2,
            ),
            # Private project routes (JWT required)
            ExpectedPipelineEndpoint(
                method="POST",
                path="/projects/create",
                has_auth=True,
                has_request_body=True,
                min_responses=2,
            ),
            ExpectedPipelineEndpoint(
                method="PUT",
                path="/projects/update/{id}",
                has_auth=True,
                has_request_body=True,
                param_names=["id"],
                min_responses=2,
            ),
            ExpectedPipelineEndpoint(
                method="DELETE",
                path="/projects/delete/{id}",
                has_auth=True,
                has_request_body=False,
                param_names=["id"],
                min_responses=2,
            ),
            # Public project routes (no auth)
            ExpectedPipelineEndpoint(
                method="GET",
                path="/projects",
                has_auth=False,
                has_request_body=False,
                response_schema_ref="Project",
            ),
            ExpectedPipelineEndpoint(
                method="GET",
                path="/projects/select/{id}",
                has_auth=False,
                has_request_body=False,
                param_names=["id"],
                response_schema_ref="Project",
            ),
            # Public user routes (no auth)
            ExpectedPipelineEndpoint(
                method="POST",
                path="/users/create",
                has_auth=False,
                has_request_body=True,
            ),
            # Service health routes (no auth)
            ExpectedPipelineEndpoint(
                method="GET",
                path="/liveness_check",
                has_auth=False,
                has_request_body=False,
            ),
            ExpectedPipelineEndpoint(
                method="GET",
                path="/readiness_check",
                has_auth=False,
                has_request_body=False,
            ),
        ],
        expected_schemas=[
            ExpectedSchema(
                name="User",
                min_properties=3,
                expected_properties=["username", "email", "password"],
            ),
            ExpectedSchema(
                name="Project",
                min_properties=3,
                expected_properties=["project", "description"],
            ),
        ],
        expected_security_schemes=["BearerAuth"],
        expected_servers=["localhost"],
        completeness_must_pass=[
            "has_endpoints",
            "has_security_schemes",
            "has_request_bodies",
            "has_schemas",
            "has_servers",
        ],
        max_validation_errors=0,
        max_unresolved_schemas=0,
    ),
    # -----------------------------------------------------------------------
    # 2. dotnet-clean-architecture — ASP.NET Core Minimal APIs
    #
    # 4 endpoint groups (route files):
    #   - TodoItems.cs:       GET, POST, PUT {id}, PATCH UpdateDetail/{id}, DELETE {id}
    #   - TodoLists.cs:       GET, POST, PUT {id}, DELETE {id}
    #   - Users.cs:           POST /logout + MapIdentityApi auto-generated
    #   - WeatherForecasts.cs: GET
    #
    # All endpoints require auth (RequireAuthorization on groups).
    # DTOs: TodoItemBriefDto, TodoListDto, TodoItemDto, TodosVm, etc.
    # Uses MediatR + FluentValidation (commands/queries as request bodies).
    # -----------------------------------------------------------------------
    PipelineGolden(
        repo_id="dotnet-clean-architecture",
        repo_dir="dotnet-clean-architecture",
        min_endpoints=10,  # ~15 total, allow variance from auto-generated identity endpoints
        min_schemas=0,  # Minimal API DTOs may not resolve — they're inline commands
        endpoints=[
            # TodoItems — reliably extracted with correct prefix
            ExpectedPipelineEndpoint(
                method="GET",
                path="/api/TodoItems",
                has_auth=True,
                has_request_body=False,
            ),
            ExpectedPipelineEndpoint(
                method="POST",
                path="/api/TodoItems",
                has_auth=True,
                has_request_body=True,
            ),
            ExpectedPipelineEndpoint(
                method="PUT",
                path="/api/TodoItems/{id}",
                has_auth=True,
                has_request_body=True,
                param_names=["id"],
            ),
            ExpectedPipelineEndpoint(
                method="PATCH",
                path="/api/TodoItems/UpdateDetail/{id}",
                has_auth=True,
                has_request_body=True,
                param_names=["id"],
            ),
            ExpectedPipelineEndpoint(
                method="DELETE",
                path="/api/TodoItems/{id}",
                has_auth=True,
                has_request_body=False,
                param_names=["id"],
            ),
            # WeatherForecasts
            ExpectedPipelineEndpoint(
                method="GET",
                path="/api/WeatherForecasts",
                has_auth=True,
                has_request_body=False,
            ),
            # TodoLists paths vary by LLM — not asserted here
            # Users identity endpoints (login, register, logout) are auto-generated
        ],
        expected_schemas=[],  # DTOs are inline commands — may not produce schemas
        expected_security_schemes=[],  # Auth scheme name varies
        expected_servers=["localhost"],
        completeness_must_pass=[
            "has_endpoints",
            "has_servers",
        ],
        max_validation_errors=5,  # Allow some — Minimal API patterns are unusual
        max_unresolved_schemas=10,
    ),
    # -----------------------------------------------------------------------
    # 3. dotnet-bitwarden — ASP.NET Core (massive Bitwarden server)
    #
    # 119 controllers across src/Api/ with domain-organized subdirs.
    # Uses [Authorize("Application")] class-level auth (Bearer JWT).
    # Heavy use of {id:guid} route constraints.
    # Request/response models in Models/Request/ and Models/Response/.
    #
    # Key controllers tested:
    #   - FoldersController (CRUD, 8 endpoints, no constraints)
    #   - SyncController (1 endpoint, query param)
    #   - SecurityTaskController (5 endpoints, {id:guid} constraints)
    #
    # This is a stress test: many controllers, deep DTO hierarchies,
    # route constraints, and very large codebase.
    # -----------------------------------------------------------------------
    PipelineGolden(
        repo_id="dotnet-bitwarden",
        repo_dir="dotnet-bitwarden",
        min_endpoints=50,  # Has 700+ but Scout may not find all controllers
        min_schemas=2,  # Should resolve at least some response models
        endpoints=[
            # FoldersController
            ExpectedPipelineEndpoint(
                method="GET",
                path="/folders/{id}",
                has_auth=True,
                param_names=["id"],
            ),
            ExpectedPipelineEndpoint(
                method="GET",
                path="/folders",
                has_auth=True,
            ),
            ExpectedPipelineEndpoint(
                method="POST",
                path="/folders",
                has_auth=True,
                has_request_body=True,
            ),
            ExpectedPipelineEndpoint(
                method="DELETE",
                path="/folders/{id}",
                has_auth=True,
                param_names=["id"],
            ),
            # SyncController
            ExpectedPipelineEndpoint(
                method="GET",
                path="/sync",
                has_auth=True,
            ),
            # SecurityTaskController — route constraints stripped
            ExpectedPipelineEndpoint(
                method="PATCH",
                path="/tasks/{taskId}/complete",
                has_auth=True,
                param_names=["taskId"],
            ),
            ExpectedPipelineEndpoint(
                method="GET",
                path="/tasks/{organizationId}/metrics",
                has_auth=True,
                param_names=["organizationId"],
            ),
        ],
        expected_schemas=[],  # Variable — depends on schema resolution
        expected_security_schemes=[],  # Should find at least one auth scheme
        expected_servers=["localhost"],
        completeness_must_pass=[
            "has_endpoints",
            "has_servers",
        ],
        max_validation_errors=20,  # Large repo — allow more variance
        max_unresolved_schemas=50,  # Deep DTO hierarchy, many may be unresolvable
    ),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@e2e
@pytest.mark.parametrize(
    "golden",
    PIPELINE_GOLDEN,
    ids=[g.repo_id for g in PIPELINE_GOLDEN],
)
def test_pipeline(golden: PipelineGolden, repos_root: str, llm_config: LLMConfig):
    """Run the full pipeline against a repo and verify the assembled spec."""
    repo_path = os.path.join(repos_root, golden.repo_dir)

    if not os.path.isdir(repo_path):
        pytest.skip(f"Repo not found: {repo_path}")

    result = run_pipeline(target_dir=repo_path, config=llm_config)

    # Basic sanity
    assert result.manifest is not None, "Pipeline should produce a manifest"
    assert result.yaml_str, "Pipeline should produce YAML output"
    assert result.spec, "Pipeline should produce a spec dict"

    # Run golden assertions
    assert_pipeline_match(
        spec=result.spec,
        schemas=result.schemas,
        golden=golden,
        validation_errors=result.validation.errors,
    )

    # Check completeness flags
    for check_name in golden.completeness_must_pass:
        value = getattr(result.completeness, check_name, None)
        assert value is True, (
            f"[{golden.repo_id}] Completeness check '{check_name}' should be True, "
            f"got {value}"
        )
