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
from .helpers import (
    ExpectedEndpoint,
    ExpectedPhase1Endpoint,
    Phase1Golden,
    RouteGolden,
    assert_endpoints_match,
    assert_phase1_match,
)

# ---------------------------------------------------------------------------
# Golden data — manually curated from reading each repo's source code
# ---------------------------------------------------------------------------

ROUTE_GOLDEN: list[RouteGolden] = [
    # -----------------------------------------------------------------------
    # 1a. rest-api-node — Express.js (private user routes, JWT auth)
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
        phase1=Phase1Golden(
            min_endpoints=2,
            endpoints=[
                ExpectedPhase1Endpoint(method="PUT", path="/users/update/{id}"),
                ExpectedPhase1Endpoint(method="DELETE", path="/users/delete/{id}"),
            ],
            has_auth_patterns=True,
            path_param_syntax=":",  # colon syntax — LLM may say ":param", ":id", etc.
        ),
    ),
    # -----------------------------------------------------------------------
    # 1b. rest-api-node — Express.js (private project routes, JWT auth)
    # Source: src/routes/private/project.js
    # 3 endpoints: POST create, PUT update, DELETE by id — all require JWT.
    # JSDoc has @swagger with security: [authorization: []]
    # References Project schema via $ref in JSDoc.
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="rest-api-node-private-project",
        repo_dir="rest-api-node",
        route_file="src/routes/private/project.js",
        framework="express",
        base_path="",
        min_endpoints=3,
        endpoints=[
            ExpectedEndpoint(
                method="POST",
                path="/projects/create",
                has_auth=True,
                has_request_body=True,
                min_responses=2,
            ),
            ExpectedEndpoint(
                method="PUT",
                path="/projects/update/{id}",
                has_auth=True,
                has_request_body=True,
                param_names=["id"],
                min_responses=2,
            ),
            ExpectedEndpoint(
                method="DELETE",
                path="/projects/delete/{id}",
                has_auth=True,
                has_request_body=False,
                param_names=["id"],
                min_responses=2,
            ),
        ],
        phase1=Phase1Golden(
            min_endpoints=3,
            endpoints=[
                ExpectedPhase1Endpoint(method="POST", path="/projects/create"),
                ExpectedPhase1Endpoint(method="PUT", path="/projects/update/{id}"),
                ExpectedPhase1Endpoint(method="DELETE", path="/projects/delete/{id}"),
            ],
            has_auth_patterns=True,
            path_param_syntax=":",
        ),
    ),
    # -----------------------------------------------------------------------
    # 1c. rest-api-node — Express.js (public project routes, no auth)
    # Source: src/routes/public/project.js
    # 2 endpoints: GET /projects (list), GET /projects/select/:id
    # Both public (no auth). Response schemas reference Project.
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="rest-api-node-public-project",
        repo_dir="rest-api-node",
        route_file="src/routes/public/project.js",
        framework="express",
        base_path="",
        min_endpoints=2,
        endpoints=[
            ExpectedEndpoint(
                method="GET",
                path="/projects",
                has_auth=False,
                has_request_body=False,
            ),
            ExpectedEndpoint(
                method="GET",
                path="/projects/select/{id}",
                has_auth=False,
                has_request_body=False,
                param_names=["id"],
            ),
        ],
        phase1=Phase1Golden(
            min_endpoints=2,
            endpoints=[
                ExpectedPhase1Endpoint(method="GET", path="/projects"),
                ExpectedPhase1Endpoint(method="GET", path="/projects/select/{id}"),
            ],
            has_auth_patterns=False,
            path_param_syntax=":",
        ),
    ),
    # -----------------------------------------------------------------------
    # 1d. rest-api-node — Express.js (public user routes, no auth)
    # Source: src/routes/public/user.js
    # 3 endpoints: POST /users/create, GET /users, GET /users/select/:id
    # All public (no auth). JSDoc references User schema.
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="rest-api-node-public-user",
        repo_dir="rest-api-node",
        route_file="src/routes/public/user.js",
        framework="express",
        base_path="",
        min_endpoints=3,
        endpoints=[
            ExpectedEndpoint(
                method="POST",
                path="/users/create",
                has_auth=False,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="GET",
                path="/users",
                has_auth=False,
                has_request_body=False,
            ),
            ExpectedEndpoint(
                method="GET",
                path="/users/select/{id}",
                has_auth=False,
                has_request_body=False,
                param_names=["id"],
            ),
        ],
        phase1=Phase1Golden(
            min_endpoints=3,
            endpoints=[
                ExpectedPhase1Endpoint(method="POST", path="/users/create"),
                ExpectedPhase1Endpoint(method="GET", path="/users"),
                ExpectedPhase1Endpoint(method="GET", path="/users/select/{id}"),
            ],
            has_auth_patterns=False,
            path_param_syntax=":",
        ),
    ),
    # -----------------------------------------------------------------------
    # 1e. rest-api-node — Express.js (service health routes, no auth)
    # Source: src/routes/public/service.js
    # 2 endpoints: GET /liveness_check, GET /readiness_check
    # Inline handlers (no controller import). No auth, no params, no body.
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="rest-api-node-service",
        repo_dir="rest-api-node",
        route_file="src/routes/public/service.js",
        framework="express",
        base_path="",
        min_endpoints=2,
        endpoints=[
            ExpectedEndpoint(
                method="GET",
                path="/liveness_check",
                has_auth=False,
                has_request_body=False,
            ),
            ExpectedEndpoint(
                method="GET",
                path="/readiness_check",
                has_auth=False,
                has_request_body=False,
            ),
        ],
        phase1=Phase1Golden(
            min_endpoints=2,
            endpoints=[
                ExpectedPhase1Endpoint(method="GET", path="/liveness_check"),
                ExpectedPhase1Endpoint(method="GET", path="/readiness_check"),
            ],
            has_auth_patterns=False,
        ),
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
        phase1=Phase1Golden(
            min_endpoints=7,
            endpoints=[
                ExpectedPhase1Endpoint(method="GET", path="/api/posts"),
                ExpectedPhase1Endpoint(method="GET", path="/api/posts/category/{id}"),
                ExpectedPhase1Endpoint(method="GET", path="/api/posts/tag/{id}"),
                ExpectedPhase1Endpoint(method="POST", path="/api/posts"),
                ExpectedPhase1Endpoint(method="GET", path="/api/posts/{id}"),
                ExpectedPhase1Endpoint(method="PUT", path="/api/posts/{id}"),
                ExpectedPhase1Endpoint(method="DELETE", path="/api/posts/{id}"),
            ],
            has_auth_patterns=True,
            has_auth_imports=True,
            base_prefix="/api/posts",
            path_param_syntax="{param}",
            required_import_substrings=["PreAuthorize"],
        ),
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
        phase1=Phase1Golden(
            min_endpoints=15,
            endpoints=[
                ExpectedPhase1Endpoint(method="POST", path="/api/users/login"),
                ExpectedPhase1Endpoint(method="POST", path="/api/users"),
                ExpectedPhase1Endpoint(method="GET", path="/api/user"),
                ExpectedPhase1Endpoint(method="GET", path="/api/profiles/{user}"),
                ExpectedPhase1Endpoint(method="POST", path="/api/profiles/{user}/follow"),
                ExpectedPhase1Endpoint(method="GET", path="/api/articles"),
                ExpectedPhase1Endpoint(method="POST", path="/api/articles"),
                ExpectedPhase1Endpoint(method="GET", path="/api/articles/{article}"),
                ExpectedPhase1Endpoint(method="DELETE", path="/api/articles/{article}"),
                ExpectedPhase1Endpoint(method="GET", path="/api/tags"),
            ],
            has_auth_patterns=False,
            has_auth_imports=False,
            has_auth_inference_notes=True,  # Must detect middleware group hint
            base_prefix="/api",
            path_param_syntax="{param}",
        ),
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
        phase1=Phase1Golden(
            min_endpoints=6,
            endpoints=[
                ExpectedPhase1Endpoint(method="GET", path="/articles"),
                ExpectedPhase1Endpoint(method="GET", path="/articles/feed"),
                ExpectedPhase1Endpoint(method="GET", path="/articles/{slug}"),
                ExpectedPhase1Endpoint(method="POST", path="/articles"),
                ExpectedPhase1Endpoint(method="PUT", path="/articles/{slug}"),
                ExpectedPhase1Endpoint(method="DELETE", path="/articles/{slug}"),
            ],
            has_auth_patterns=True,
            has_auth_imports=True,
            base_prefix="/articles",
            path_param_syntax="{param}",
            required_import_substrings=["Authorization", "Security"],
        ),
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
        phase1=Phase1Golden(
            min_endpoints=9,
            endpoints=[
                ExpectedPhase1Endpoint(method="POST", path="/request/magic", handler_name="request_magic"),
                ExpectedPhase1Endpoint(method="GET", path="/verify/magic", handler_name="verify_magic"),
                ExpectedPhase1Endpoint(method="POST", path="/totp/enroll", handler_name="totp_enroll"),
                ExpectedPhase1Endpoint(method="POST", path="/totp/verify", handler_name="totp_verify"),
                ExpectedPhase1Endpoint(method="POST", path="/token/refresh", handler_name="refresh_token"),
                ExpectedPhase1Endpoint(method="POST", path="/webauthn/register/options"),
                ExpectedPhase1Endpoint(method="POST", path="/webauthn/register/complete"),
                ExpectedPhase1Endpoint(method="POST", path="/webauthn/login/options"),
                ExpectedPhase1Endpoint(method="POST", path="/webauthn/login/complete"),
            ],
            has_auth_patterns=False,
            has_auth_imports=True,  # jwt import is auth-related
        ),
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
        phase1=Phase1Golden(
            min_endpoints=4,
            endpoints=[
                ExpectedPhase1Endpoint(method="POST", path="/schemas/import", handler_name="import_schema"),
                ExpectedPhase1Endpoint(method="GET", path="/schemas/{application}/{service}/versions", handler_name="list_versions"),
                ExpectedPhase1Endpoint(method="GET", path="/schemas/{application}/{service}/latest", handler_name="get_latest"),
                ExpectedPhase1Endpoint(method="GET", path="/schemas/{application}/{service}/{version}", handler_name="get_version"),
            ],
            has_auth_patterns=False,
            has_auth_imports=False,
            base_prefix="/schemas",
            path_param_syntax="{param}",
            required_import_substrings=["fastapi", "UploadFile", "app.database", "app.services"],
        ),
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
        phase1=Phase1Golden(
            min_endpoints=3,
            endpoints=[
                ExpectedPhase1Endpoint(method="GET", path="/"),
                ExpectedPhase1Endpoint(method="GET", path="/search"),
                ExpectedPhase1Endpoint(method="GET", path="/searchab"),
            ],
            has_auth_patterns=False,
            has_auth_imports=False,
        ),
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
        phase1=Phase1Golden(
            min_endpoints=1,
            endpoints=[
                ExpectedPhase1Endpoint(method="GET", path="/energy/history"),
            ],
            has_auth_patterns=True,  # Should detect authorizer?.claims pattern
            has_auth_imports=False,
            required_import_substrings=["aws-lambda", "DynamoDB"],
        ),
    ),
    # -----------------------------------------------------------------------
    # 9. dotnet-clean-architecture — ASP.NET Core Minimal APIs (TodoItems.cs)
    # Uses IEndpointGroup with MapGet/MapPost/MapPut/MapPatch/MapDelete.
    # Route prefix: /api/TodoItems (derived from class name).
    # All endpoints require authorization via RequireAuthorization().
    # 5 endpoints: GET (paginated), POST, PUT {id}, PATCH UpdateDetail/{id},
    #              DELETE {id}.
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="dotnet-clean-architecture-todoitems",
        repo_dir="dotnet-clean-architecture",
        route_file="src/Web/Endpoints/TodoItems.cs",
        framework="aspnetcore",
        base_path="/api/TodoItems",
        min_endpoints=5,
        endpoints=[
            ExpectedEndpoint(
                method="GET",
                path="/api/TodoItems",
                has_auth=True,
                has_request_body=False,
            ),
            ExpectedEndpoint(
                method="POST",
                path="/api/TodoItems",
                has_auth=True,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="PUT",
                path="/api/TodoItems/{id}",
                has_auth=True,
                has_request_body=True,
                param_names=["id"],
            ),
            ExpectedEndpoint(
                method="PATCH",
                path="/api/TodoItems/UpdateDetail/{id}",
                has_auth=True,
                has_request_body=True,
                param_names=["id"],
            ),
            ExpectedEndpoint(
                method="DELETE",
                path="/api/TodoItems/{id}",
                has_auth=True,
                has_request_body=False,
                param_names=["id"],
            ),
        ],
        phase1=Phase1Golden(
            min_endpoints=5,
            endpoints=[
                ExpectedPhase1Endpoint(method="GET", path="/api/TodoItems"),
                ExpectedPhase1Endpoint(method="POST", path="/api/TodoItems"),
                ExpectedPhase1Endpoint(method="PUT", path="/api/TodoItems/{id}"),
                ExpectedPhase1Endpoint(method="PATCH", path="/api/TodoItems/UpdateDetail/{id}"),
                ExpectedPhase1Endpoint(method="DELETE", path="/api/TodoItems/{id}"),
            ],
            has_auth_patterns=True,  # RequireAuthorization()
            base_prefix="/api/TodoItems",
            path_param_syntax="{",
        ),
    ),
    # -----------------------------------------------------------------------
    # 10. dotnet-clean-architecture — ASP.NET Core Minimal APIs (TodoLists.cs)
    # Route prefix: /api/TodoLists.
    # All endpoints require authorization. 4 endpoints: GET, POST, PUT, DELETE.
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="dotnet-clean-architecture-todolists",
        repo_dir="dotnet-clean-architecture",
        route_file="src/Web/Endpoints/TodoLists.cs",
        framework="aspnetcore",
        base_path="/api/TodoLists",
        min_endpoints=4,
        endpoints=[
            ExpectedEndpoint(
                method="GET",
                path="/api/TodoLists",
                has_auth=True,
                has_request_body=False,
            ),
            ExpectedEndpoint(
                method="POST",
                path="/api/TodoLists",
                has_auth=True,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="PUT",
                path="/api/TodoLists/{id}",
                has_auth=True,
                has_request_body=True,
                param_names=["id"],
            ),
            ExpectedEndpoint(
                method="DELETE",
                path="/api/TodoLists/{id}",
                has_auth=True,
                has_request_body=False,
                param_names=["id"],
            ),
        ],
        phase1=Phase1Golden(
            min_endpoints=4,
            endpoints=[
                ExpectedPhase1Endpoint(method="GET", path="/api/TodoLists"),
                ExpectedPhase1Endpoint(method="POST", path="/api/TodoLists"),
                ExpectedPhase1Endpoint(method="PUT", path="/api/TodoLists/{id}"),
                ExpectedPhase1Endpoint(method="DELETE", path="/api/TodoLists/{id}"),
            ],
            has_auth_patterns=True,
            base_prefix="/api/TodoLists",
            path_param_syntax="{",
        ),
    ),
    # -----------------------------------------------------------------------
    # 11. dotnet-bitwarden — ASP.NET Core (FoldersController.cs)
    # Traditional [ApiController] with [Route("folders")] and
    # class-level [Authorize("Application")].
    # 8 endpoints: GET {id}, GET (list all), POST, PUT {id},
    #              POST {id} (deprecated PUT), DELETE {id},
    #              POST {id}/delete (deprecated DELETE), DELETE all.
    # No route constraints in this controller — {id} is string.
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="dotnet-bitwarden-folders",
        repo_dir="dotnet-bitwarden",
        route_file="src/Api/Vault/Controllers/FoldersController.cs",
        framework="aspnetcore",
        base_path="",
        min_endpoints=6,  # Allow some variance — deprecated endpoints may be skipped
        endpoints=[
            ExpectedEndpoint(
                method="GET",
                path="/folders/{id}",
                has_auth=True,
                has_request_body=False,
                param_names=["id"],
            ),
            ExpectedEndpoint(
                method="GET",
                path="/folders",
                has_auth=True,
                has_request_body=False,
            ),
            ExpectedEndpoint(
                method="POST",
                path="/folders",
                has_auth=True,
                has_request_body=True,
            ),
            ExpectedEndpoint(
                method="PUT",
                path="/folders/{id}",
                has_auth=True,
                has_request_body=True,
                param_names=["id"],
            ),
            ExpectedEndpoint(
                method="DELETE",
                path="/folders/{id}",
                has_auth=True,
                has_request_body=False,
                param_names=["id"],
            ),
            ExpectedEndpoint(
                method="DELETE",
                path="/folders/all",
                has_auth=True,
                has_request_body=False,
            ),
        ],
        phase1=Phase1Golden(
            min_endpoints=6,
            endpoints=[
                ExpectedPhase1Endpoint(method="GET", path="/folders/{id}"),
                ExpectedPhase1Endpoint(method="GET", path="/folders"),
                ExpectedPhase1Endpoint(method="POST", path="/folders"),
                ExpectedPhase1Endpoint(method="PUT", path="/folders/{id}"),
                ExpectedPhase1Endpoint(method="DELETE", path="/folders/{id}"),
                ExpectedPhase1Endpoint(method="DELETE", path="/folders/all"),
            ],
            has_auth_patterns=True,  # [Authorize("Application")] on class
            has_auth_imports=True,
            base_prefix="/folders",
            path_param_syntax="{",
        ),
    ),
    # -----------------------------------------------------------------------
    # 12. dotnet-bitwarden — ASP.NET Core (SecurityTaskController.cs)
    # [Route("tasks")] with [Authorize("Application")].
    # Has {taskId:guid} and {organizationId:guid} route constraints.
    # 5 endpoints: GET (list), PATCH {taskId:guid}/complete,
    #              GET organization (query param), GET {organizationId:guid}/metrics,
    #              POST {orgId:guid}/bulk-create.
    # Tests route constraint handling.
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="dotnet-bitwarden-securitytask",
        repo_dir="dotnet-bitwarden",
        route_file="src/Api/Vault/Controllers/SecurityTaskController.cs",
        framework="aspnetcore",
        base_path="",
        min_endpoints=5,
        endpoints=[
            ExpectedEndpoint(
                method="GET",
                path="/tasks",
                has_auth=True,
                has_request_body=False,
                # status is an optional [FromQuery] param — LLM may miss it
            ),
            ExpectedEndpoint(
                method="PATCH",
                path="/tasks/{taskId}/complete",
                has_auth=True,
                has_request_body=False,
                param_names=["taskId"],
            ),
            ExpectedEndpoint(
                method="GET",
                path="/tasks/organization",
                has_auth=True,
                has_request_body=False,
                param_names=["organizationId"],
            ),
            ExpectedEndpoint(
                method="GET",
                path="/tasks/{organizationId}/metrics",
                has_auth=True,
                has_request_body=False,
                param_names=["organizationId"],
            ),
            ExpectedEndpoint(
                method="POST",
                path="/tasks/{orgId}/bulk-create",
                has_auth=True,
                has_request_body=True,
                param_names=["orgId"],
            ),
        ],
        phase1=Phase1Golden(
            min_endpoints=5,
            endpoints=[
                ExpectedPhase1Endpoint(method="GET", path="/tasks"),
                ExpectedPhase1Endpoint(method="PATCH", path="/tasks/{taskId}/complete"),
                ExpectedPhase1Endpoint(method="GET", path="/tasks/organization"),
                ExpectedPhase1Endpoint(method="GET", path="/tasks/{organizationId}/metrics"),
                ExpectedPhase1Endpoint(method="POST", path="/tasks/{orgId}/bulk-create"),
            ],
            has_auth_patterns=True,
            has_auth_imports=True,
            base_prefix="/tasks",
            path_param_syntax="{",
        ),
    ),
    # -----------------------------------------------------------------------
    # 13. dotnet-bitwarden — ASP.NET Core (SyncController.cs)
    # [Route("sync")] with [Authorize("Application")].
    # Single endpoint: GET with excludeDomains query param.
    # Tests simple controller with class-level auth.
    # -----------------------------------------------------------------------
    RouteGolden(
        repo_id="dotnet-bitwarden-sync",
        repo_dir="dotnet-bitwarden",
        route_file="src/Api/Vault/Controllers/SyncController.cs",
        framework="aspnetcore",
        base_path="",
        min_endpoints=1,
        endpoints=[
            ExpectedEndpoint(
                method="GET",
                path="/sync",
                has_auth=True,
                has_request_body=False,
                param_names=["excludeDomains"],
            ),
        ],
        phase1=Phase1Golden(
            min_endpoints=1,
            endpoints=[
                ExpectedPhase1Endpoint(method="GET", path="/sync"),
            ],
            has_auth_patterns=True,
            has_auth_imports=True,
            base_prefix="/sync",
            path_param_syntax=None,  # No path params in this controller
        ),
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

    # Phase 1 assertions (intermediate — catches silent failures early)
    if golden.phase1 is not None:
        assert record.code_analysis_obj is not None, (
            f"[{golden.repo_id}] Phase 1 analysis object not available in run record"
        )
        assert_phase1_match(record.code_analysis_obj, golden.phase1, golden.repo_id)

    # Phase 2 assertions (final output)
    assert_endpoints_match(descriptor, golden)
