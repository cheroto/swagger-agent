"""E2E tests for the Scout agent.

One test per repo with manually curated golden data.
Tests run the actual LLM (via Scout harness) and assert
structural properties of the DiscoveryManifest output.

Run: pytest tests/e2e/test_scout.py -m e2e -v
"""

from __future__ import annotations

import os

import pytest

from swagger_agent.agents.scout.harness import run_scout
from swagger_agent.config import LLMConfig

from .conftest import e2e
from .helpers import ScoutGolden, assert_scout_match

# ---------------------------------------------------------------------------
# Golden data — manually curated from reading each repo's source code
# ---------------------------------------------------------------------------

SCOUT_GOLDEN: list[ScoutGolden] = [
    # -----------------------------------------------------------------------
    # 1. rest-api-node — Express.js (consign-based route loading)
    # Route files are in src/routes/public/ and src/routes/private/
    # 5 route files total. Server on port 8080.
    # -----------------------------------------------------------------------
    ScoutGolden(
        repo_id="rest-api-node",
        repo_dir="rest-api-node",
        framework="express",
        language="javascript",
        route_files=[
            "src/routes/public/service.js",
            "src/routes/public/user.js",
            "src/routes/public/project.js",
            "src/routes/private/user.js",
            "src/routes/private/project.js",
        ],
        min_route_files=5,
        servers=["localhost:8080"],
        base_path="",
    ),
    # -----------------------------------------------------------------------
    # 2. spring-boot-blog — Spring Boot (9 @RestController classes)
    # All controllers under src/main/java/.../controller/
    # Default port 8080. Base path /api.
    # -----------------------------------------------------------------------
    ScoutGolden(
        repo_id="spring-boot-blog",
        repo_dir="spring-boot-blog",
        framework="spring",
        language="java",
        route_files=[
            "src/main/java/com/sopromadze/blogapi/controller/AuthController.java",
            "src/main/java/com/sopromadze/blogapi/controller/AlbumController.java",
            "src/main/java/com/sopromadze/blogapi/controller/PostController.java",
            "src/main/java/com/sopromadze/blogapi/controller/UserController.java",
            "src/main/java/com/sopromadze/blogapi/controller/CategoryController.java",
            "src/main/java/com/sopromadze/blogapi/controller/CommentController.java",
            "src/main/java/com/sopromadze/blogapi/controller/PhotoController.java",
            "src/main/java/com/sopromadze/blogapi/controller/TagController.java",
            "src/main/java/com/sopromadze/blogapi/controller/TodoController.java",
        ],
        min_route_files=9,
        servers=["localhost:8080"],
        base_path="/api",
    ),
    # -----------------------------------------------------------------------
    # 3. laravel-realworld — Laravel (routes/api.php)
    # All API routes in one file. API prefix /api from RouteServiceProvider.
    # Server from APP_URL: http://localhost
    # -----------------------------------------------------------------------
    ScoutGolden(
        repo_id="laravel-realworld",
        repo_dir="laravel-realworld",
        framework="laravel",
        language="php",
        route_files=[
            "routes/api.php",
        ],
        min_route_files=1,
        servers=["localhost"],
        base_path="/api",
    ),
    # -----------------------------------------------------------------------
    # 4. aspnetcore-realworld — ASP.NET Core (8 controller classes)
    # Controllers in src/Conduit/Features/*/
    # Default port 5000.
    # -----------------------------------------------------------------------
    ScoutGolden(
        repo_id="aspnetcore-realworld",
        repo_dir="aspnetcore-realworld",
        framework="aspnetcore",
        language="csharp",
        route_files=[
            "src/Conduit/Features/Articles/ArticlesController.cs",
            "src/Conduit/Features/Comments/CommentsController.cs",
            "src/Conduit/Features/Favorites/FavoritesController.cs",
            "src/Conduit/Features/Followers/FollowersController.cs",
            "src/Conduit/Features/Profiles/ProfilesController.cs",
            "src/Conduit/Features/Tags/TagsController.cs",
            "src/Conduit/Features/Users/UserController.cs",
            "src/Conduit/Features/Users/UsersController.cs",
        ],
        min_route_files=8,
        servers=["localhost"],
        base_path="",
    ),
    # -----------------------------------------------------------------------
    # 5. passwordless-auth-rust — Axum (3 route files + main.rs)
    # Routes in src/routes.rs, src/admin.rs, src/metrics.rs
    # main.rs composes them. Server on port 3000.
    # -----------------------------------------------------------------------
    ScoutGolden(
        repo_id="passwordless-auth-rust",
        repo_dir="passwordless-auth-rust",
        framework="axum",
        language="rust",
        route_files=[
            "src/routes.rs",
            "src/admin.rs",
            "src/metrics.rs",
        ],
        min_route_files=3,
        servers=["localhost:3000"],
        base_path="",
    ),
    # -----------------------------------------------------------------------
    # 6. levo-schema-service — FastAPI (2 route files)
    # Routes in Code/app/routes.py and Code/app/main.py
    # Server on localhost:8000.
    # Note: repo root contains Code/ directory.
    # -----------------------------------------------------------------------
    ScoutGolden(
        repo_id="levo-schema-service",
        repo_dir="levo-schema-service",
        framework="fastapi",
        language="python",
        route_files=[
            "Code/app/routes.py",
        ],
        min_route_files=1,
        servers=["localhost"],
        base_path="",
    ),
    # -----------------------------------------------------------------------
    # 7. 9jauni — Go net/http (single main.go)
    # All routes registered in main.go via http.HandleFunc.
    # Server on port 8080.
    # -----------------------------------------------------------------------
    ScoutGolden(
        repo_id="9jauni",
        repo_dir="9jauni",
        framework="go",
        language="go",
        route_files=[
            "main.go",
        ],
        min_route_files=1,
        servers=["localhost:8080"],
        base_path="",
    ),
    # -----------------------------------------------------------------------
    # 8. energy-monitoring-app — AWS Lambda/CDK (6 handler files)
    # Handler files in src/handlers/auth/ and src/handlers/energy/
    # Routes defined in CDK stack (lib/energy-monitoring-app-stack.ts)
    # No fixed server URL (API Gateway).
    # -----------------------------------------------------------------------
    ScoutGolden(
        repo_id="energy-monitoring-app",
        repo_dir="energy-monitoring-app",
        framework="aws",
        language="typescript",
        route_files=[
            "src/handlers/auth/signup.ts",
            "src/handlers/auth/signin.ts",
            "src/handlers/energy/input-handler.ts",
            "src/handlers/energy/get-upload-url.ts",
            "src/handlers/energy/get-history.ts",
            "src/handlers/energy/manage-alerts.ts",
        ],
        min_route_files=4,
        servers=[],  # API Gateway URL is dynamic
        base_path="",
    ),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@e2e
@pytest.mark.parametrize(
    "golden",
    SCOUT_GOLDEN,
    ids=[g.repo_id for g in SCOUT_GOLDEN],
)
def test_scout_discovery(golden: ScoutGolden, repos_root: str, llm_config: LLMConfig):
    """Run the Scout against a repo and verify the discovery manifest."""
    repo_path = os.path.join(repos_root, golden.repo_dir)

    if not os.path.isdir(repo_path):
        pytest.skip(f"Repo not found: {repo_path}")

    manifest, record = run_scout(
        target_dir=repo_path,
        config=llm_config,
    )

    # Basic sanity — Scout must terminate with write_artifact
    assert record.termination_reason == "write_artifact", (
        f"[{golden.repo_id}] Scout terminated with '{record.termination_reason}' "
        f"after {len(record.turns)} turns, expected 'write_artifact'"
    )

    # Must not take too many turns (indicates spinning)
    assert len(record.turns) <= 30, (
        f"[{golden.repo_id}] Scout took {len(record.turns)} turns (max 30)"
    )

    # Golden data assertions
    assert_scout_match(manifest, golden)
