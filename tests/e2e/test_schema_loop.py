"""E2E tests for the schema resolution loop.

Tests the full chain: ref_hint resolution (ctags + grep) -> Schema Extractor
(LLM call) -> recursive $ref following. Golden data provides curated ref_hints
(simulating route extractor output) and expected schema properties.

Run: pytest tests/e2e/test_schema_loop.py -m e2e -v

Requires: universal-ctags installed, LLM server running, test repos available.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from swagger_agent.config import LLMConfig
from swagger_agent.infra.schema_loop import run_schema_loop

from .conftest import e2e
from .helpers import ExpectedSchema, SchemaLoopGolden, assert_schemas_match

# ---------------------------------------------------------------------------
# Golden data — manually curated ref_hints and expected schemas
# ---------------------------------------------------------------------------

SCHEMA_GOLDEN: list[SchemaLoopGolden] = [
    # -----------------------------------------------------------------------
    # 1. rest-api-node — Mongoose models
    # Route endpoints reference User schema via JSDoc $ref.
    # Model file: src/app/Models/User.js — Mongoose schema with
    # fields: username (String, required), email (String, required, format email),
    # password (String, required), photo (String), nickname (String).
    # The Project model is in a separate file.
    # -----------------------------------------------------------------------
    SchemaLoopGolden(
        repo_id="rest-api-node",
        repo_dir="rest-api-node",
        framework="express",
        ref_hints=[
            {
                "ref_hint": "User",
                "import_source": "const User = require('../../app/Models/User')",
                "resolution": "import",
            },
            {
                "ref_hint": "Project",
                "import_source": "const Project = require('../../app/Models/Project')",
                "resolution": "import",
            },
        ],
        min_schemas=2,
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
    ),
    # -----------------------------------------------------------------------
    # 2. levo-schema-service — SQLAlchemy models
    # Route endpoints reference Application, Service, Schema models.
    # Model file: Code/app/models.py — SQLAlchemy models.
    # Application: id (Integer, PK), name (String, unique)
    # Service: id (Integer, PK), name (String), application_id (Integer, FK)
    # Schema: id (Integer, PK), version (Integer), file_path (String),
    #         uploaded_at (DateTime), application_id (FK), service_id (FK, nullable)
    # Cross-references: Service -> Application, Schema -> Application, Schema -> Service
    # -----------------------------------------------------------------------
    SchemaLoopGolden(
        repo_id="levo-schema-service",
        repo_dir="levo-schema-service",
        framework="fastapi",
        ref_hints=[
            {
                "ref_hint": "Application",
                "import_source": "from app.models import Application",
                "resolution": "import",
            },
            {
                "ref_hint": "Schema",
                "import_source": "from app.models import Schema",
                "resolution": "import",
            },
        ],
        min_schemas=2,  # At minimum Application and Schema; Service may also appear
        expected_schemas=[
            ExpectedSchema(
                name="Application",
                min_properties=2,
                expected_properties=["id", "name"],
            ),
            ExpectedSchema(
                name="Schema",
                min_properties=4,
                expected_properties=["id", "version", "file_path"],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # 3. passwordless-auth-rust — Rust structs
    # The route file (routes.rs) defines request/response structs inline:
    # RequestMagicBody { email }, VerifyQuery { token }, AuthResponse { access_token,
    # refresh_token }, TotpEnrollBody { email }, TotpEnrollResp { secret, otpauth_url },
    # TotpVerifyBody { email, code }, RefreshBody { refresh_token },
    # WebauthnRegisterOptionsBody { email }, WebauthnRegisterCompleteBody { pending_id,
    # response }, WebauthnLoginOptionsBody { email }, WebauthnLoginCompleteBody
    # { pending_id, response }.
    # DB models in src/models.rs: User { id, email, totp_secret, created_at },
    # MagicLink { token, user_id, expires_at, used },
    # RefreshToken { token, user_id, expires_at, revoked, created_at }.
    # Testing ref_hints for the inline request/response types (defined in routes.rs).
    # -----------------------------------------------------------------------
    SchemaLoopGolden(
        repo_id="passwordless-auth-rust",
        repo_dir="passwordless-auth-rust",
        framework="axum",
        ref_hints=[
            {
                "ref_hint": "RequestMagicBody",
                "import_source": None,
                "resolution": "class_to_file",
            },
            {
                "ref_hint": "AuthResponse",
                "import_source": None,
                "resolution": "class_to_file",
            },
            {
                "ref_hint": "TotpVerifyBody",
                "import_source": None,
                "resolution": "class_to_file",
            },
            {
                "ref_hint": "RefreshBody",
                "import_source": None,
                "resolution": "class_to_file",
            },
        ],
        min_schemas=3,
        expected_schemas=[
            ExpectedSchema(
                name="RequestMagicBody",
                min_properties=1,
                expected_properties=["email"],
            ),
            ExpectedSchema(
                name="AuthResponse",
                min_properties=2,
                expected_properties=["access_token", "refresh_token"],
            ),
            ExpectedSchema(
                name="TotpVerifyBody",
                min_properties=2,
                expected_properties=["email", "code"],
            ),
            ExpectedSchema(
                name="RefreshBody",
                min_properties=1,
                expected_properties=["refresh_token"],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # 4. 9jauni — Go structs in main.go
    # Structs: uniRequest { Name, Abbreviation, WebsiteLink },
    #          errorResponse { Message, Code }
    # JSON tags: name, abbreviation, website_link, message, code
    # -----------------------------------------------------------------------
    SchemaLoopGolden(
        repo_id="9jauni",
        repo_dir="9jauni",
        framework="go-net-http",
        ref_hints=[
            {
                "ref_hint": "uniRequest",
                "import_source": None,
                "resolution": "class_to_file",
            },
            {
                "ref_hint": "errorResponse",
                "import_source": None,
                "resolution": "class_to_file",
            },
        ],
        min_schemas=2,
        expected_schemas=[
            ExpectedSchema(
                name="uniRequest",
                min_properties=2,
                # Properties may use Go field names or JSON tags
                expected_properties=[],
            ),
            ExpectedSchema(
                name="errorResponse",
                min_properties=2,
                expected_properties=[],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # 5. spring-boot-blog — Java models
    # PostController references: PostRequest (request body), PostResponse,
    # Post (entity), ApiResponse. These are in separate files under
    # model/ and payload/ directories.
    # Post model has: id, title, body, createdAt, updatedAt, user (User ref),
    # category (Category ref), tags (list of Tag refs), comments (list).
    # -----------------------------------------------------------------------
    SchemaLoopGolden(
        repo_id="spring-boot-blog",
        repo_dir="spring-boot-blog",
        framework="spring",
        ref_hints=[
            {
                "ref_hint": "Post",
                "import_source": "import com.sopromadze.blogapi.model.Post",
                "resolution": "import",
            },
            {
                "ref_hint": "PostRequest",
                "import_source": "import com.sopromadze.blogapi.payload.PostRequest",
                "resolution": "import",
            },
        ],
        min_schemas=1,  # At minimum Post; PostRequest may resolve or not
        expected_schemas=[
            ExpectedSchema(
                name="Post",
                min_properties=3,
                expected_properties=["title", "body"],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # 6. aspnetcore-realworld — C# records and classes
    # ASP.NET Core vertical-slice architecture with MediatR.
    # Types live in the same namespace as controllers (no explicit using
    # statement needed). Envelope types are simple records/classes wrapping
    # domain objects. Tests that ctags-based resolution works for C#
    # where import-path resolution cannot help (no Python/JS-style imports).
    #
    # ArticleEnvelope.cs: record ArticleEnvelope(Article Article)
    # ArticlesEnvelope.cs: class ArticlesEnvelope { List<Article> Articles; int ArticlesCount; }
    # TagsEnvelope.cs: class TagsEnvelope { List<string> Tags; }
    # ProfileEnvelope.cs: record ProfileEnvelope(Profile Profile)
    # User.cs: record UserEnvelope(User User) — note: envelope defined in same
    #          file as the User class.
    #
    # All use class_to_file resolution — the Route Extractor should recognize
    # these as same-namespace types with no explicit import.
    # -----------------------------------------------------------------------
    SchemaLoopGolden(
        repo_id="aspnetcore-realworld",
        repo_dir="aspnetcore-realworld",
        framework="aspnetcore",
        ref_hints=[
            {
                "ref_hint": "ArticleEnvelope",
                "import_source": None,
                "resolution": "class_to_file",
            },
            {
                "ref_hint": "ArticlesEnvelope",
                "import_source": None,
                "resolution": "class_to_file",
            },
            {
                "ref_hint": "TagsEnvelope",
                "import_source": None,
                "resolution": "class_to_file",
            },
            {
                "ref_hint": "ProfileEnvelope",
                "import_source": None,
                "resolution": "class_to_file",
            },
            {
                "ref_hint": "UserEnvelope",
                "import_source": None,
                "resolution": "class_to_file",
            },
        ],
        min_schemas=3,
        expected_schemas=[
            ExpectedSchema(
                name="ArticleEnvelope",
                min_properties=1,
                # Record with a single Article property
                expected_properties=[],
            ),
            ExpectedSchema(
                name="ArticlesEnvelope",
                min_properties=1,
                # Class with Articles list and ArticlesCount
                expected_properties=[],
            ),
            ExpectedSchema(
                name="TagsEnvelope",
                min_properties=1,
                # Class with Tags list (List<string>)
                expected_properties=[],
            ),
            ExpectedSchema(
                name="ProfileEnvelope",
                min_properties=1,
                expected_properties=[],
            ),
            ExpectedSchema(
                name="UserEnvelope",
                min_properties=1,
                expected_properties=[],
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
    SCHEMA_GOLDEN,
    ids=[g.repo_id for g in SCHEMA_GOLDEN],
)
def test_schema_loop(golden: SchemaLoopGolden, repos_root: str, llm_config: LLMConfig):
    """Run the schema resolution loop with curated ref_hints and verify output."""
    # Check ctags is available
    if not shutil.which("ctags"):
        pytest.skip("universal-ctags not installed")

    project_root = Path(repos_root) / golden.repo_dir
    if not project_root.is_dir():
        pytest.skip(f"Repo not found: {project_root}")

    schemas, _inheritance_map = run_schema_loop(
        ref_hints=golden.ref_hints,
        framework=golden.framework,
        project_root=project_root,
        config=llm_config,
        max_depth=5,
    )

    # Golden assertions
    assert_schemas_match(schemas, golden)

    # Every schema should be a valid dict with at least "type"
    for name, schema in schemas.items():
        assert isinstance(schema, dict), f"Schema '{name}' is not a dict"
        if not schema.get("x-unresolved"):
            assert "type" in schema or "properties" in schema or "$ref" in schema, (
                f"Schema '{name}' has no type, properties, or $ref"
            )
