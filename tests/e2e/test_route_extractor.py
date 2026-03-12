"""E2E tests for the Route Extractor agent.

One test per repo, each with manually curated golden data.
Tests run the actual LLM (via route extractor harness) and assert
structural properties of the output.

Run: pytest tests/e2e/test_route_extractor.py -m e2e -v
"""

from __future__ import annotations

import os

import pytest

from swagger_agent.agents.route_extractor.harness import (
    RouteExtractorContext,
    run_route_extractor,
)
from swagger_agent.config import LLMConfig

from .conftest import e2e
from .helpers import ExpectedEndpoint, RouteGolden, assert_endpoints_match

# ---------------------------------------------------------------------------
# Golden data — manually curated from reading each repo's source code
# ---------------------------------------------------------------------------

ROUTE_GOLDEN: list[RouteGolden] = [
    # -----------------------------------------------------------------------
    # 1. rest-api-node — Express.js (private user routes, JWT auth)
    # Source: src/routes/private/user.js
    # Routes use consign; private routes have JWT Bearer middleware.
    # Code: src.put("/users/update/:id", ...), src.delete("/users/delete/:id", ...)
    # Also has @swagger JSDoc with security: [authorization: []]
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="rest-api-node",
        repo_dir="rest-api-node",
        route_file="src/routes/private/user.js",
        framework="express",
        base_path="",
        min_endpoints=2,
        endpoints=[
            ExpectedEndpoint(
                method="PUT",
                path="/users/update/{id}",
                has_auth=True,
                has_request_body=True,
                param_names=["id"],
                min_responses=2,
            ),
            ExpectedEndpoint(
                method="DELETE",
                path="/users/delete/{id}",
                has_auth=True,
                has_request_body=False,
                param_names=["id"],
                min_responses=2,
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # 2. spring-boot-blog — Spring Boot (PostController)
    # Source: src/main/java/com/sopromadze/blogapi/controller/PostController.java
    # @RequestMapping("/api/posts")
    # 7 endpoints: GET list, GET by category, GET by tag, POST create,
    #              GET by id, PUT update, DELETE
    # Auth: @PreAuthorize("hasRole('USER')") on POST
    #        @PreAuthorize("hasRole('USER') or hasRole('ADMIN')") on PUT, DELETE
    #        GET endpoints are public
    # Paginated endpoints have page/size @RequestParam
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="spring-boot-blog",
        repo_dir="spring-boot-blog",
        route_file="src/main/java/com/sopromadze/blogapi/controller/PostController.java",
        framework="spring",
        base_path="/api",
        min_endpoints=7,
        endpoints=[
            ExpectedEndpoint(
                method="GET",
                path="/api/posts",
                has_auth=False,
                has_request_body=False,
                param_names=["page", "size"],
            ),
            ExpectedEndpoint(
                method="GET",
                path="/api/posts/category/{id}",
                has_auth=False,
                has_request_body=False,
                param_names=["id"],
            ),
            ExpectedEndpoint(
                method="GET",
                path="/api/posts/tag/{id}",
                has_auth=False,
                has_request_body=False,
                param_names=["id"],
            ),
            ExpectedEndpoint(
                method="POST",
                path="/api/posts",
                has_auth=True,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="GET",
                path="/api/posts/{id}",
                has_auth=False,
                has_request_body=False,
                param_names=["id"],
            ),
            ExpectedEndpoint(
                method="PUT",
                path="/api/posts/{id}",
                has_auth=True,
                has_request_body=True,
                param_names=["id"],
            ),
            ExpectedEndpoint(
                method="DELETE",
                path="/api/posts/{id}",
                has_auth=True,
                has_request_body=False,
                param_names=["id"],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # 3. laravel-realworld — Laravel (routes/api.php)
    # All API routes defined in one file using Route::group with middleware.
    # Public: GET articles, GET articles/{article}, GET tags, login, register, etc.
    # Protected (auth.api middleware): CRUD articles, comments, favorites, follow
    # 21 routes total.
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="laravel-realworld",
        repo_dir="laravel-realworld",
        route_file="routes/api.php",
        framework="laravel",
        base_path="/api",
        min_endpoints=15,  # Conservative — some may be tricky to extract
        endpoints=[
            # Auth
            ExpectedEndpoint(
                method="POST",
                path="/api/users/login",
                has_auth=False,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="POST",
                path="/api/users",
                has_auth=False,
                has_request_body=True,
            ),
            # User
            ExpectedEndpoint(
                method="GET",
                path="/api/user",
                has_auth=True,
                has_request_body=False,
            ),
            # Articles
            ExpectedEndpoint(
                method="GET",
                path="/api/articles",
                has_auth=None,  # Optional auth
                has_request_body=False,
            ),
            ExpectedEndpoint(
                method="POST",
                path="/api/articles",
                has_auth=True,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="DELETE",
                path="/api/articles/{article}",
                has_auth=True,
                has_request_body=False,
                param_names=["article"],
            ),
            # Favorites
            ExpectedEndpoint(
                method="POST",
                path="/api/articles/{article}/favorite",
                has_auth=True,
                param_names=["article"],
            ),
            # Comments
            ExpectedEndpoint(
                method="POST",
                path="/api/articles/{article}/comments",
                has_auth=True,
                has_request_body=True,
                param_names=["article"],
            ),
            ExpectedEndpoint(
                method="GET",
                path="/api/articles/{article}/comments",
                has_auth=None,
                param_names=["article"],
            ),
            # Tags
            ExpectedEndpoint(
                method="GET",
                path="/api/tags",
                has_auth=False,
                has_request_body=False,
            ),
            # Profiles
            ExpectedEndpoint(
                method="POST",
                path="/api/profiles/{user}/follow",
                has_auth=True,
                param_names=["user"],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # 4. aspnetcore-realworld — ASP.NET Core (ArticlesController.cs)
    # [Route("articles")]
    # 6 endpoints: GET list, GET feed, GET by slug, POST create, PUT edit, DELETE
    # Auth via [Authorize(AuthenticationSchemes = JwtIssuerOptions.Schemes)]
    # on POST, PUT, DELETE. GET list and GET by slug are public.
    # GET feed is also authorized.
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="aspnetcore-realworld",
        repo_dir="aspnetcore-realworld",
        route_file="src/Conduit/Features/Articles/ArticlesController.cs",
        framework="aspnetcore",
        base_path="",
        min_endpoints=6,
        endpoints=[
            ExpectedEndpoint(
                method="GET",
                path="/articles",
                has_auth=False,
                has_request_body=False,
                param_names=["tag", "author", "favorited"],
            ),
            ExpectedEndpoint(
                method="GET",
                path="/articles/feed",
                has_auth=None,  # Technically no [Authorize] on this method in code
                has_request_body=False,
            ),
            ExpectedEndpoint(
                method="GET",
                path="/articles/{slug}",
                has_auth=False,
                has_request_body=False,
                param_names=["slug"],
            ),
            ExpectedEndpoint(
                method="POST",
                path="/articles",
                has_auth=True,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="PUT",
                path="/articles/{slug}",
                has_auth=True,
                has_request_body=True,
                param_names=["slug"],
            ),
            ExpectedEndpoint(
                method="DELETE",
                path="/articles/{slug}",
                has_auth=True,
                has_request_body=False,
                param_names=["slug"],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # 5. passwordless-auth-rust — Axum (routes.rs)
    # 9 endpoints: request_magic, verify_magic, totp_enroll, totp_verify,
    #              refresh_token, webauthn register/complete, login options/complete
    # All public (auth is what these endpoints *provide*, not require).
    # All POST except verify_magic (GET with query param).
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="passwordless-auth-rust",
        repo_dir="passwordless-auth-rust",
        route_file="src/routes.rs",
        framework="axum",
        base_path="",
        min_endpoints=9,
        endpoints=[
            ExpectedEndpoint(
                method="POST",
                path="/request/magic",
                has_auth=False,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="GET",
                path="/verify/magic",
                has_auth=False,
                has_request_body=False,
                param_names=["token"],
            ),
            ExpectedEndpoint(
                method="POST",
                path="/totp/enroll",
                has_auth=False,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="POST",
                path="/totp/verify",
                has_auth=False,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="POST",
                path="/token/refresh",
                has_auth=False,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="POST",
                path="/webauthn/register/options",
                has_auth=False,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="POST",
                path="/webauthn/register/complete",
                has_auth=False,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="POST",
                path="/webauthn/login/options",
                has_auth=False,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="POST",
                path="/webauthn/login/complete",
                has_auth=False,
                has_request_body=True,
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # 6. levo-schema-service — FastAPI (routes.py)
    # 4 endpoints: POST /schemas/import (multipart), GET versions, GET latest,
    #              GET by version. All public. Path params for application/service.
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="levo-schema-service",
        repo_dir="levo-schema-service",
        route_file="Code/app/routes.py",
        framework="fastapi",
        base_path="",
        min_endpoints=4,
        endpoints=[
            ExpectedEndpoint(
                method="POST",
                path="/schemas/import",
                has_auth=False,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="GET",
                path="/schemas/{application}/{service}/versions",
                has_auth=False,
                has_request_body=False,
                param_names=["application", "service"],
            ),
            ExpectedEndpoint(
                method="GET",
                path="/schemas/{application}/{service}/latest",
                has_auth=False,
                has_request_body=False,
                param_names=["application", "service"],
            ),
            ExpectedEndpoint(
                method="GET",
                path="/schemas/{application}/{service}/{version}",
                has_auth=False,
                has_request_body=False,
                param_names=["application", "service", "version"],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # 7. 9jauni — Go net/http (main.go)
    # 3 endpoints: GET / (list all), GET /search (POST body with name),
    #              GET /searchab (query param abbreviation)
    # All public, no auth.
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="9jauni",
        repo_dir="9jauni",
        route_file="main.go",
        framework="go-net-http",
        base_path="",
        min_endpoints=3,
        endpoints=[
            ExpectedEndpoint(
                method="GET",
                path="/",
                has_auth=False,
                has_request_body=False,
            ),
            ExpectedEndpoint(
                method="GET",
                path="/search",
                has_auth=False,
                # The /search endpoint reads JSON body despite being GET
                has_request_body=None,
            ),
            ExpectedEndpoint(
                method="GET",
                path="/searchab",
                has_auth=False,
                has_request_body=False,
                param_names=["abbreviation"],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # 8. energy-monitoring-app — AWS Lambda (get-history.ts)
    # Single Lambda handler. No route decorators — routes are in infra config.
    # The handler code shows: GET semantics, query params startDate/endDate,
    # auth via event.requestContext.authorizer, DynamoDB query.
    # This is a challenging case — the route extractor must infer from Lambda code.
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="energy-monitoring-app",
        repo_dir="energy-monitoring-app",
        route_file="src/handlers/energy/get-history.ts",
        framework="aws-lambda",
        base_path="",
        min_endpoints=1,
        endpoints=[
            ExpectedEndpoint(
                method="GET",
                path="/energy/history",
                has_auth=True,
                has_request_body=False,
                param_names=["startDate", "endDate"],
                min_responses=2,
            ),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@e2e
@pytest.mark.parametrize(
    "golden",
    ROUTE_GOLDEN,
    ids=[g.repo_id for g in ROUTE_GOLDEN],
)
def test_route_extraction(golden: RouteGolden, repos_root: str, llm_config: LLMConfig):
    """Run the Route Extractor against a repo's route file and verify output."""
    repo_path = os.path.join(repos_root, golden.repo_dir)
    route_file = os.path.join(repo_path, golden.route_file)

    if not os.path.isfile(route_file):
        pytest.skip(f"Route file not found: {route_file}")

    context = RouteExtractorContext(
        framework=golden.framework,
        base_path=golden.base_path,
        target_file=route_file,
    )

    descriptor, record = run_route_extractor(
        target_file=route_file,
        context=context,
        config=llm_config,
    )

    # Basic sanity
    assert descriptor.source_file == route_file
    assert record.endpoint_count == len(descriptor.endpoints)

    # Golden assertions
    assert_endpoints_match(descriptor, golden)
