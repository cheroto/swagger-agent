"""Golden tests for the deterministic prescan against all test repos.

These tests run without an LLM — they verify that the heuristic-based
prescan correctly identifies framework, language, route files, servers,
and base path from project config files and source code patterns.

Run: pytest tests/test_prescan.py -v
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest

from swagger_agent.infra.detectors import run_prescan
from swagger_agent.infra.detectors.result import PrescanResult


# ---------------------------------------------------------------------------
# Golden data model
# ---------------------------------------------------------------------------


@dataclass
class PrescanGolden:
    """Golden expectations for prescan output on a specific repo."""

    repo_id: str
    repo_dir: str

    # Framework detection
    framework: str | None  # Expected framework (exact match), None = not detectable
    language: str | None  # Expected language (exact match), None = not detectable

    # Route files
    expected_route_files: list[str] = field(default_factory=list)  # Must be found
    min_route_files: int = 0  # Minimum count
    unexpected_route_files: list[str] = field(default_factory=list)  # Must NOT be found

    # Servers
    expected_server_substrings: list[str] = field(default_factory=list)  # Must appear in at least one server URL
    min_servers: int = 0

    # Base path
    base_path: str | None = None  # Expected base_path, None = don't check

    # Notes — optional description of known prescan limitations for this repo
    known_limitations: str = ""


# ---------------------------------------------------------------------------
# Golden data — calibrated against actual prescan output
# ---------------------------------------------------------------------------

PRESCAN_GOLDEN: list[PrescanGolden] = [
    # -----------------------------------------------------------------------
    # 1. rest-api-node — Express.js
    # Prescan finds express from package.json. Only finds src/config/express.js
    # (the consign route loader) because actual route files in src/routes/
    # use module.exports = (app) => { app.get(...) } which the regex catches.
    # The real route files need the Scout to follow consign's directory loading.
    # -----------------------------------------------------------------------
    PrescanGolden(
        repo_id="rest-api-node",
        repo_dir="rest-api-node",
        framework="express",
        language="javascript",
        expected_route_files=["src/config/express.js"],
        min_route_files=1,
        expected_server_substrings=["localhost:3000"],
        min_servers=1,
        known_limitations=(
            "Only finds the consign route loader, not the actual route files "
            "in src/routes/. Scout is needed to follow consign directory paths."
        ),
    ),
    # -----------------------------------------------------------------------
    # 2. spring-boot-blog — Spring Boot (9 controllers)
    # Prescan nails this one: all 9 controllers found via @*Mapping annotations.
    # -----------------------------------------------------------------------
    PrescanGolden(
        repo_id="spring-boot-blog",
        repo_dir="spring-boot-blog",
        framework="spring",
        language="java",
        expected_route_files=[
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
        expected_server_substrings=["localhost:8080"],
        min_servers=1,
    ),
    # -----------------------------------------------------------------------
    # 3. laravel-realworld — Laravel
    # Finds routes/api.php (correct). Also finds routes/web.php (extra but
    # acceptable — it does contain Route:: calls). Base path /api is set in
    # RouteServiceProvider, not detectable by prescan's simple patterns.
    # -----------------------------------------------------------------------
    PrescanGolden(
        repo_id="laravel-realworld",
        repo_dir="laravel-realworld",
        framework="laravel",
        language="php",
        expected_route_files=["routes/api.php"],
        min_route_files=1,
        expected_server_substrings=["localhost"],
        min_servers=1,
        known_limitations=(
            "Cannot detect /api base path (set in RouteServiceProvider). "
            "Also picks up routes/web.php as a route file."
        ),
    ),
    # -----------------------------------------------------------------------
    # 4. aspnetcore-realworld — ASP.NET Core
    # Finds 4 of 8 controllers. Missing controllers (Comments, Favorites,
    # Followers, Profiles) likely don't use [Http*] attributes directly or
    # use a different routing pattern.
    # -----------------------------------------------------------------------
    PrescanGolden(
        repo_id="aspnetcore-realworld",
        repo_dir="aspnetcore-realworld",
        framework="aspnetcore",
        language="csharp",
        expected_route_files=[
            "src/Conduit/Features/Articles/ArticlesController.cs",
            "src/Conduit/Features/Tags/TagsController.cs",
            "src/Conduit/Features/Users/UserController.cs",
            "src/Conduit/Features/Users/UsersController.cs",
        ],
        min_route_files=4,
        expected_server_substrings=["localhost:5000"],
        min_servers=1,
        known_limitations=(
            "Finds 4/8 controllers. Missing controllers may use MediatR "
            "dispatching without [Http*] attributes."
        ),
    ),
    # -----------------------------------------------------------------------
    # 5. passwordless-auth-rust — Axum
    # Prescan finds framework correctly. Route patterns are overly broad for
    # Axum (.get/.post/.route) — catches many non-route files like db.rs,
    # session.rs, etc. The core route files are present in the results.
    # -----------------------------------------------------------------------
    PrescanGolden(
        repo_id="passwordless-auth-rust",
        repo_dir="passwordless-auth-rust",
        framework="axum",
        language="rust",
        expected_route_files=[
            "src/routes.rs",
            "src/admin.rs",
            "src/metrics.rs",
            "src/main.rs",
        ],
        min_route_files=3,
        expected_server_substrings=["localhost:8080"],
        min_servers=1,
        known_limitations=(
            "Axum route patterns are broad — picks up many non-route files "
            "like db.rs, session.rs that happen to use .get()/.route() methods."
        ),
    ),
    # -----------------------------------------------------------------------
    # 6. levo-schema-service — FastAPI
    # No requirements.txt or pyproject.toml at the repo root — the Python
    # project lives under Code/. Prescan cannot detect the framework.
    # -----------------------------------------------------------------------
    PrescanGolden(
        repo_id="levo-schema-service",
        repo_dir="levo-schema-service",
        framework=None,
        language=None,
        min_route_files=0,
        min_servers=0,
        known_limitations=(
            "Python project is nested under Code/ with no root-level "
            "requirements.txt or pyproject.toml. Prescan cannot detect framework."
        ),
    ),
    # -----------------------------------------------------------------------
    # 7. 9jauni — Go net/http
    # Framework detected from go.mod. Route files not found because main.go
    # is at the repo root and the glob pattern **/*.go doesn't match root-
    # level files (fnmatch ** doesn't match zero path segments).
    # -----------------------------------------------------------------------
    PrescanGolden(
        repo_id="9jauni",
        repo_dir="9jauni",
        framework="net/http",
        language="go",
        min_route_files=0,
        expected_server_substrings=["localhost:8080"],
        min_servers=1,
        known_limitations=(
            "main.go at repo root not matched by **/*.go glob pattern. "
            "fnmatch ** requires at least one directory segment."
        ),
    ),
    # -----------------------------------------------------------------------
    # 8. energy-monitoring-app — AWS Lambda/CDK
    # No recognized framework. This is a CDK project with Lambda handlers,
    # not a traditional web framework. Prescan finds package.json but no
    # known framework dependency.
    # -----------------------------------------------------------------------
    PrescanGolden(
        repo_id="energy-monitoring-app",
        repo_dir="energy-monitoring-app",
        framework=None,
        language=None,
        min_route_files=0,
        min_servers=0,
        known_limitations=(
            "AWS Lambda/CDK project — not a recognized web framework. "
            "Routes are defined in CDK stack, not via framework decorators."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "golden",
    PRESCAN_GOLDEN,
    ids=[g.repo_id for g in PRESCAN_GOLDEN],
)
def test_prescan_detection(golden: PrescanGolden, repos_root: str):
    """Run deterministic prescan against a repo and verify against golden data."""
    repo_path = os.path.join(repos_root, golden.repo_dir)

    if not os.path.isdir(repo_path):
        pytest.skip(f"Repo not found: {repo_path}")

    result = run_prescan(repo_path)

    # --- Framework ---
    if golden.framework is not None:
        assert result.framework == golden.framework, (
            f"[{golden.repo_id}] Expected framework '{golden.framework}', "
            f"got '{result.framework}'"
        )
    else:
        assert result.framework is None, (
            f"[{golden.repo_id}] Expected no framework detection, "
            f"got '{result.framework}'"
        )

    # --- Language ---
    if golden.language is not None:
        assert result.language == golden.language, (
            f"[{golden.repo_id}] Expected language '{golden.language}', "
            f"got '{result.language}'"
        )
    else:
        assert result.language is None, (
            f"[{golden.repo_id}] Expected no language detection, "
            f"got '{result.language}'"
        )

    # --- Route files: minimum count ---
    assert len(result.route_files) >= golden.min_route_files, (
        f"[{golden.repo_id}] Expected at least {golden.min_route_files} route files, "
        f"got {len(result.route_files)}: {result.route_files}"
    )

    # --- Route files: each expected file must be present ---
    for expected_file in golden.expected_route_files:
        assert expected_file in result.route_files, (
            f"[{golden.repo_id}] Expected route file '{expected_file}' not found. "
            f"Got: {result.route_files}"
        )

    # --- Route files: unexpected files must NOT be present ---
    for unexpected_file in golden.unexpected_route_files:
        assert unexpected_file not in result.route_files, (
            f"[{golden.repo_id}] Unexpected route file '{unexpected_file}' was found. "
            f"Got: {result.route_files}"
        )

    # --- Servers ---
    assert len(result.servers) >= golden.min_servers, (
        f"[{golden.repo_id}] Expected at least {golden.min_servers} server(s), "
        f"got {len(result.servers)}: {result.servers}"
    )

    for expected_sub in golden.expected_server_substrings:
        found = any(expected_sub in s for s in result.servers)
        assert found, (
            f"[{golden.repo_id}] Expected server URL containing '{expected_sub}' "
            f"not found. Got: {result.servers}"
        )

    # --- Base path ---
    if golden.base_path is not None:
        assert result.base_path == golden.base_path, (
            f"[{golden.repo_id}] Expected base_path='{golden.base_path}', "
            f"got '{result.base_path}'"
        )
