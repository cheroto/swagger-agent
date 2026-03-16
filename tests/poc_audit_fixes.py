#!/usr/bin/env python3
"""Proof-of-Concept tests for INFRA_AUDIT fixes.

Runs targeted LLM calls against the actual qwen3.5 model to verify
proposed schema/prompt changes produce correct output.

Usage:
    python tests/poc_audit_fixes.py
"""

from __future__ import annotations

import json
import sys
import os
import time
from pathlib import Path
from typing import Literal

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import instructor
from openai import OpenAI
from pydantic import BaseModel, Field

from swagger_agent.config import LLMConfig


# ── Setup ──

config = LLMConfig()
raw_client = OpenAI(base_url=config.llm_base_url, api_key=config.llm_api_key)
mode = {
    "tools": instructor.Mode.TOOLS,
    "json": instructor.Mode.JSON,
    "json_schema": instructor.Mode.JSON_SCHEMA,
    "md_json": instructor.Mode.MD_JSON,
}[config.instructor_mode.lower()]
client = instructor.from_openai(raw_client, mode=mode)

PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"
WARN = "\033[93m⚠ WARN\033[0m"

results = []


def run_test(name: str, fn):
    """Run a test function, catch exceptions, report result."""
    print(f"\n{'='*70}")
    print(f"TEST: {name}")
    print(f"{'='*70}")
    try:
        passed, details = fn()
        status = PASS if passed else FAIL
        print(f"\n{status} {name}")
        if details:
            print(f"  {details}")
        results.append((name, passed, details))
    except Exception as e:
        print(f"\n{FAIL} {name}")
        print(f"  Exception: {e}")
        results.append((name, False, str(e)))


def call_llm(system: str, user: str, response_model, max_tokens=4096):
    """Make an instructor call to the LLM."""
    start = time.monotonic()
    result = client.chat.completions.create(
        model=config.llm_model,
        response_model=response_model,
        max_retries=config.instructor_max_retries,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=config.llm_temperature,
        max_tokens=max_tokens,
        **config.extra_create_kwargs(),
    )
    elapsed = (time.monotonic() - start) * 1000
    print(f"  LLM call: {elapsed:.0f}ms")
    return result


# ============================================================================
# TEST 1: SchemaProperty WITH constraints field
# ============================================================================
# The audit says SchemaProperty has no constraints field, which means the LLM
# cannot output minLength, maxLength, pattern, etc. Adding a constraints dict
# should let the LLM output these directly, eliminating ~20 lines of fixup code.

class SchemaPropertyWithConstraints(BaseModel):
    """A single property on a data model / DTO / entity."""
    name: str = Field(description="Property name as serialized in JSON.")
    type: str = Field(description="JSON Schema type: 'string', 'integer', 'number', 'boolean', 'array', 'object'.")
    format: str = Field(default="", description="JSON Schema format: 'date-time', 'email', 'uuid', 'uri', 'binary', 'int64'. Empty if none.")
    ref: str = Field(default="", description="Referenced schema name for complex types. Empty for primitives.")
    is_array: bool = Field(default=False, description="True if list/array/set of the type.")
    nullable: bool = Field(default=False, description="True if accepts null/None/nil.")
    enum_values: list[str] = Field(default_factory=list, description="Enum values if enum type.")
    constraints: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "Validation constraints from code annotations/decorators/validators. "
            "Use standard JSON Schema keywords: minLength, maxLength, pattern, "
            "minimum, maximum, exclusiveMinimum, exclusiveMaximum, multipleOf, "
            "minItems, maxItems, uniqueItems. "
            "Example: {'minLength': 3, 'maxLength': 50, 'pattern': '^[a-zA-Z]+$'}"
        ),
    )


class ExtractedSchemaWithConstraints(BaseModel):
    name: str = Field(description="Class/struct/record/model name exactly as defined in code.")
    properties: list[SchemaPropertyWithConstraints] = Field(description="ALL data-carrying fields on this model.")
    required_fields: list[str] = Field(default_factory=list, description="Names of mandatory fields.")
    parent_ref: str = Field(default="", description="Parent class name if inheriting from a known_schema type.")


class SchemaDescriptorWithConstraints(BaseModel):
    source_file: str = ""
    schemas: list[ExtractedSchemaWithConstraints] = Field(description="Every model/entity/DTO/record class in this file.")


SCHEMA_PROMPT = """\
You are the Schema Extractor agent. Extract every data model, DTO, entity, and record class from this file.
Fill every field in the response schema. All field semantics are defined in the schema descriptions.
When a property references a type in known_schemas, set ref to its name. For same-file types, extract as sibling schemas and reference via ref.
"""


def test_constraints_extraction_python():
    """Test that adding constraints dict to SchemaProperty extracts validation rules from Python/Pydantic."""
    code = '''\
from pydantic import BaseModel, Field, field_validator
from typing import Optional

class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=30, pattern=r'^[a-zA-Z0-9_]+$')
    email: str = Field(..., max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    age: Optional[int] = Field(None, ge=13, le=120)
    bio: Optional[str] = Field(None, max_length=500)
'''
    context = json.dumps({"framework": "fastapi", "target_file": "app/schemas/user.py", "known_schemas": {}}, indent=2)
    user_msg = f"## Context\n\n```json\n{context}\n```\n\n## Model File: app/schemas/user.py\n\n```python\n{code}\n```"

    result = call_llm(SCHEMA_PROMPT, user_msg, SchemaDescriptorWithConstraints)

    schemas = {s.name: s for s in result.schemas}
    print(f"  Schemas found: {list(schemas.keys())}")

    if "CreateUserRequest" not in schemas:
        return False, "CreateUserRequest not found"

    schema = schemas["CreateUserRequest"]
    props = {p.name: p for p in schema.properties}
    print(f"  Properties: {list(props.keys())}")

    issues = []
    # Check username constraints
    if "username" in props:
        c = props["username"].constraints
        print(f"  username constraints: {c}")
        if not c:
            issues.append("username has no constraints")
        else:
            if c.get("minLength") != 3 and c.get("min_length") != 3:
                issues.append(f"username minLength wrong: {c}")
            if c.get("maxLength") != 30 and c.get("max_length") != 30:
                issues.append(f"username maxLength wrong: {c}")
    else:
        issues.append("username not found")

    # Check password constraints
    if "password" in props:
        c = props["password"].constraints
        print(f"  password constraints: {c}")
        if not c:
            issues.append("password has no constraints")
    else:
        issues.append("password not found")

    # Check age constraints
    if "age" in props:
        c = props["age"].constraints
        print(f"  age constraints: {c}")
        if not c:
            issues.append("age has no constraints")
    else:
        issues.append("age not found")

    if issues:
        return False, "; ".join(issues)
    return True, f"All constraints extracted correctly"


def test_constraints_extraction_kotlin():
    """Test constraint extraction from Kotlin data class with validation annotations."""
    code = '''\
package com.example.models

import jakarta.validation.constraints.*

data class RegisterUserRequest(
    @field:NotBlank
    @field:Size(min = 3, max = 50)
    @field:Email
    val email: String,

    @field:NotBlank
    @field:Size(min = 8, max = 128)
    val password: String,

    @field:NotBlank
    @field:Size(min = 1, max = 100)
    val username: String,

    @field:Min(0)
    @field:Max(150)
    val age: Int? = null
)
'''
    context = json.dumps({"framework": "ktor", "target_file": "src/models/User.kt", "known_schemas": {}}, indent=2)
    user_msg = f"## Context\n\n```json\n{context}\n```\n\n## Model File: src/models/User.kt\n\n```kotlin\n{code}\n```"

    result = call_llm(SCHEMA_PROMPT, user_msg, SchemaDescriptorWithConstraints)

    schemas = {s.name: s for s in result.schemas}
    print(f"  Schemas found: {list(schemas.keys())}")

    if "RegisterUserRequest" not in schemas:
        return False, "RegisterUserRequest not found"

    schema = schemas["RegisterUserRequest"]
    props = {p.name: p for p in schema.properties}
    print(f"  Properties: {list(props.keys())}")

    has_constraints = sum(1 for p in props.values() if p.constraints)
    total = len(props)
    print(f"  Properties with constraints: {has_constraints}/{total}")

    for name, prop in props.items():
        print(f"    {name}: constraints={prop.constraints}")

    if has_constraints == 0:
        return False, "No constraints extracted from Kotlin annotations"
    return True, f"{has_constraints}/{total} properties have constraints"


# ============================================================================
# TEST 2: Go struct extraction (BUG 7 — empty properties)
# ============================================================================

class SchemaPropertyBasic(BaseModel):
    name: str = Field(description="Property name as serialized in JSON. Use the alias if a serialization annotation provides one (@JsonProperty, @SerializedName, alias=, CodingKeys). Skip fields with exclusion annotations (@JsonIgnore, [JsonIgnore], @Transient, @Expose(serialize:false)).")
    type: str = Field(description="JSON Schema type: 'string', 'integer', 'number', 'boolean', 'array', 'object'.")
    format: str = Field(default="", description="JSON Schema format when applicable: 'date-time', 'date', 'email', 'uuid', 'uri', 'binary', 'int64', 'float', 'double'. Empty string if none.")
    ref: str = Field(default="", description="Referenced schema name for complex types. Empty for primitives.")
    is_array: bool = Field(default=False, description="True if list/array/set of the type.")
    nullable: bool = Field(default=False, description="True if accepts null/None/nil.")
    enum_values: list[str] = Field(default_factory=list, description="Enum values if enum type.")


class ExtractedSchemaBasic(BaseModel):
    name: str = Field(description="Class/struct/record/model name exactly as defined in code.")
    properties: list[SchemaPropertyBasic] = Field(description="ALL data-carrying fields on this model. Every model has at least one field — if you cannot identify fields, the type is likely not a data model. Include inherited fields unless the parent is in known_schemas (then use parent_ref instead).")
    required_fields: list[str] = Field(default_factory=list, description="Names of mandatory fields.")
    parent_ref: str = Field(default="", description="Parent class name if inheriting from a known_schema type.")


class SchemaDescriptorBasic(BaseModel):
    source_file: str = ""
    schemas: list[ExtractedSchemaBasic] = Field(description="Every model/entity/DTO/record class in this file.")


def test_go_struct_extraction():
    """Test Schema Extractor on Go struct with GORM tags (BUG 7 reproduction)."""
    code = '''\
package models

import (
    "github.com/jinzhu/gorm"
)

type User struct {
    gorm.Model
    FirstName string `gorm:"column:first_name;type:varchar(255);not null" json:"first_name"`
    LastName  string `gorm:"column:last_name;type:varchar(255);not null" json:"last_name"`
    Email     string `gorm:"column:email;type:varchar(255);unique_index" json:"email"`
    Password  string `gorm:"column:password;not null" json:"-"`
    IsAdmin   bool   `gorm:"column:is_admin;default:false" json:"is_admin"`
    Bio       string `gorm:"column:bio;type:text" json:"bio,omitempty"`
}

type Product struct {
    gorm.Model
    Name        string  `gorm:"not null" json:"name"`
    Slug        string  `gorm:"unique_index" json:"slug"`
    Description string  `gorm:"type:text" json:"description"`
    Price       float64 `gorm:"not null" json:"price"`
    Stock       int     `gorm:"default:0" json:"stock"`
    CategoryID  uint    `json:"category_id"`
}
'''
    context = json.dumps({
        "framework": "gin",
        "target_file": "models/user.go",
        "known_schemas": {},
    }, indent=2)
    user_msg = f"## Context\n\n```json\n{context}\n```\n\n## Model File: models/user.go\n\n```go\n{code}\n```"

    result = call_llm(SCHEMA_PROMPT, user_msg, SchemaDescriptorBasic)

    schemas = {s.name: s for s in result.schemas}
    print(f"  Schemas found: {list(schemas.keys())}")

    issues = []
    for name in ["User", "Product"]:
        if name not in schemas:
            issues.append(f"{name} not found")
            continue
        props = [p.name for p in schemas[name].properties]
        print(f"  {name} properties: {props}")
        if len(props) == 0:
            issues.append(f"{name} has ZERO properties — BUG 7 reproduced")
        elif len(props) < 3:
            issues.append(f"{name} only has {len(props)} properties (expected 5+)")

    if issues:
        return False, "; ".join(issues)
    return True, f"User has {len(schemas['User'].properties)} props, Product has {len(schemas['Product'].properties)} props"


# ============================================================================
# TEST 3: Kotlin data class extraction (BUG — all schemas have empty properties)
# ============================================================================

def test_kotlin_data_class_extraction():
    """Test Schema Extractor on Kotlin data class (same-package, no imports)."""
    code = '''\
package io.realworld.domain

data class User(
    val id: Long? = null,
    val email: String,
    val token: String? = null,
    val username: String,
    val bio: String? = null,
    val image: String? = null,
    val password: String? = null
)

data class Article(
    val id: Long? = null,
    val slug: String,
    val title: String,
    val description: String,
    val body: String,
    val tagList: List<String> = emptyList(),
    val createdAt: String? = null,
    val updatedAt: String? = null,
    val favorited: Boolean = false,
    val favoritesCount: Int = 0,
    val author: User? = null
)
'''
    context = json.dumps({
        "framework": "ktor",
        "target_file": "src/main/kotlin/io/realworld/domain/User.kt",
        "known_schemas": {},
    }, indent=2)
    user_msg = f"## Context\n\n```json\n{context}\n```\n\n## Model File: src/main/kotlin/io/realworld/domain/User.kt\n\n```kotlin\n{code}\n```"

    result = call_llm(SCHEMA_PROMPT, user_msg, SchemaDescriptorBasic)

    schemas = {s.name: s for s in result.schemas}
    print(f"  Schemas found: {list(schemas.keys())}")

    issues = []
    for name, expected_min in [("User", 5), ("Article", 8)]:
        if name not in schemas:
            issues.append(f"{name} not found")
            continue
        props = [p.name for p in schemas[name].properties]
        print(f"  {name} properties ({len(props)}): {props}")
        if len(props) < expected_min:
            issues.append(f"{name} has {len(props)} properties, expected {expected_min}+")

    if issues:
        return False, "; ".join(issues)

    # Check that Article references User
    article = schemas["Article"]
    author_prop = next((p for p in article.properties if p.name == "author"), None)
    if author_prop and author_prop.ref == "User":
        print("  Article.author correctly references User ✓")
    elif author_prop:
        print(f"  Article.author ref='{author_prop.ref}' (expected 'User')")

    return True, f"User has {len(schemas['User'].properties)} props, Article has {len(schemas['Article'].properties)} props"


# ============================================================================
# TEST 4: Clojure defschema extraction (known hard case)
# ============================================================================

def test_clojure_schema_extraction():
    """Test Schema Extractor on Clojure defschema (BUG — map literal syntax)."""
    code = '''\
(ns pizza-service.domain
  (:require [schema.core :as s]))

(s/defschema Topping
  (s/enum :pepperoni :mushrooms :onions :sausage :bacon))

(s/defschema Pizza
  {:id Long
   :name String
   :price Double
   :hot Boolean
   (s/optional-key :description) String
   :toppings #{Topping}})

(s/defschema NewPizza
  {:name String
   :price Double
   :hot Boolean
   (s/optional-key :description) String
   :toppings #{Topping}})
'''
    context = json.dumps({
        "framework": "compojure",
        "target_file": "src/pizza_service/domain.clj",
        "known_schemas": {},
    }, indent=2)
    user_msg = f"## Context\n\n```json\n{context}\n```\n\n## Model File: src/pizza_service/domain.clj\n\n```clojure\n{code}\n```"

    result = call_llm(SCHEMA_PROMPT, user_msg, SchemaDescriptorBasic)

    schemas = {s.name: s for s in result.schemas}
    print(f"  Schemas found: {list(schemas.keys())}")

    issues = []
    for name, expected_min in [("Pizza", 4), ("NewPizza", 3)]:
        if name not in schemas:
            issues.append(f"{name} not found")
            continue
        props = [p.name for p in schemas[name].properties]
        print(f"  {name} properties ({len(props)}): {props}")
        if len(props) < expected_min:
            issues.append(f"{name} has {len(props)} properties, expected {expected_min}+")

    # Check Topping enum
    if "Topping" in schemas:
        t = schemas["Topping"]
        print(f"  Topping: properties={[p.name for p in t.properties]}, enum check...")
        # Topping is an enum — might have enum_values on a single property or be represented differently
        if t.properties and t.properties[0].enum_values:
            print(f"    enum_values: {t.properties[0].enum_values}")

    if issues:
        return False, "; ".join(issues)
    return True, "Clojure schemas extracted with properties"


# ============================================================================
# TEST 5: RefHint is_array field usage
# ============================================================================
# The audit says _COLLECTION_WRAPPERS parsing exists because LLM doesn't use
# is_array. Test if the LLM reliably sets is_array=True when appropriate.

class RefHintTest(BaseModel):
    ref_hint: str = Field(description="Type name as it appears in code. Use the inner type only, strip collection wrappers: List<Article> → 'Article'.")
    resolution: Literal["import", "class_to_file", "unresolvable"] = Field(description="'import' = found the import statement. 'class_to_file' = same namespace. 'unresolvable' = builtin/external type.")
    import_line: str = Field(default="", description="The exact import statement. Only when resolution='import'.")
    is_array: bool = Field(default=False, description="True if the original type was a collection/list/array wrapper (List<T>, T[], IEnumerable<T>). ref_hint contains the inner element type only.")
    is_nullable: bool = Field(default=False, description="True if the original type was optional/nullable (Optional[T], T?, T | null). ref_hint contains the inner type only.")


class ResponseTest(BaseModel):
    status_code: str = Field(description="HTTP status code.")
    description: str = ""
    schema_ref: RefHintTest = Field(description="Type reference for the response body.")


class EndpointTest(BaseModel):
    method: str = Field(description="HTTP method.")
    path: str = Field(description="Full path in OpenAPI {param} syntax.")
    operation_id: str = Field(description="Handler function name.")
    responses: list[ResponseTest] = Field(default_factory=list)


class EndpointDescriptorTest(BaseModel):
    source_file: str = ""
    endpoints: list[EndpointTest] = Field(description="Every endpoint.")


def test_refhint_is_array():
    """Test that LLM sets is_array=True on RefHint for collection return types."""
    code = '''\
from fastapi import APIRouter
from typing import List
from app.schemas.article import Article, ArticleResponse
from app.schemas.user import User

router = APIRouter()

@router.get("/articles")
def list_articles() -> List[Article]:
    pass

@router.get("/articles/{slug}")
def get_article(slug: str) -> ArticleResponse:
    pass

@router.get("/users")
def list_users() -> list[User]:
    pass
'''
    prompt = """\
You are the Route Extractor agent. Extract every HTTP endpoint from this route file into structured format.

## Code Observations
- Routing style: decorator-based
- Path parameter syntax: {param}
- Base prefix: /api
- Request bodies: N/A
- Error handling: N/A

## Available Imports (for RefHint resolution)
```
from app.schemas.article import Article, ArticleResponse
from app.schemas.user import User
```
Use these exact import lines as import_line in RefHints when a type matches.

## Strategy
- Combine base_path + router-level prefix + endpoint-level path.
- Every path parameter segment in the URL must have a matching Parameter object.
- Extract ALL endpoints in the file.
"""
    context = json.dumps({"framework": "fastapi", "base_path": "/api", "target_file": "app/routes/articles.py"}, indent=2)
    user_msg = f"## Context\n\n```json\n{context}\n```\n\n## Route File: app/routes/articles.py\n\n```python\n{code}\n```"

    result = call_llm(prompt, user_msg, EndpointDescriptorTest)

    endpoints = {f"{e.method} {e.path}": e for e in result.endpoints}
    print(f"  Endpoints: {list(endpoints.keys())}")

    issues = []

    # Check list_articles returns array
    list_ep = None
    for key, ep in endpoints.items():
        if "articles" in key and ep.method == "GET" and "{" not in key:
            list_ep = ep
            break

    if list_ep:
        for resp in list_ep.responses:
            ref = resp.schema_ref
            print(f"  list_articles response: ref_hint='{ref.ref_hint}', is_array={ref.is_array}, resolution='{ref.resolution}'")
            if ref.ref_hint == "Article" and ref.is_array:
                print("    ✓ Correctly stripped List wrapper and set is_array=True")
            elif "List" in ref.ref_hint or "list" in ref.ref_hint:
                issues.append(f"list_articles: LLM kept wrapper in ref_hint: '{ref.ref_hint}'")
            elif not ref.is_array:
                issues.append(f"list_articles: is_array=False (should be True)")
            break
    else:
        issues.append("list_articles endpoint not found")

    # Check list_users returns array
    users_ep = None
    for key, ep in endpoints.items():
        if "users" in key and ep.method == "GET":
            users_ep = ep
            break

    if users_ep:
        for resp in users_ep.responses:
            ref = resp.schema_ref
            print(f"  list_users response: ref_hint='{ref.ref_hint}', is_array={ref.is_array}")
            if not ref.is_array and ref.ref_hint in ("User", "user"):
                issues.append("list_users: is_array=False (should be True)")
            break

    if issues:
        return False, "; ".join(issues)
    return True, "LLM correctly uses is_array field for collection types"


# ============================================================================
# TEST 6: Security scheme type accuracy (BUG 13)
# ============================================================================

class SecurityReqTest(BaseModel):
    name: str = Field(description="Security scheme name.")
    scheme_type: Literal["bearer", "apikey", "basic", "oauth2", "cookie"] = Field(
        description=(
            "Auth mechanism. 'bearer' for JWT/token in Authorization header. "
            "'apikey' for API key in header/query/cookie (including cookie-based session auth). "
            "'basic' for HTTP Basic. 'oauth2' for OAuth2 flows. "
            "'cookie' for cookie-based authentication/sessions."
        ),
    )


class EndpointSecTest(BaseModel):
    method: str
    path: str
    operation_id: str
    security: list[SecurityReqTest] = Field(default_factory=list)


class EndpointDescriptorSecTest(BaseModel):
    source_file: str = ""
    endpoints: list[EndpointSecTest] = Field(description="Every endpoint.")


def test_cookie_auth_scheme_type():
    """Test that cookie/session auth is identified as 'apikey' or 'cookie', not 'bearer'."""
    code = '''\
open Dream

let () =
  Dream.run
  @@ Dream.logger
  @@ Dream.cookie_sessions
  @@ Dream.router [
    Dream.get "/api/user"
      (fun request ->
        match Dream.session "user_id" request with
        | None -> Dream.json ~status:`Unauthorized {|{"error": "Not logged in"}|}
        | Some user_id -> Dream.json {|{"id": "|} ^ user_id ^ {|"}|});

    Dream.post "/api/login"
      (fun request ->
        let%lwt body = Dream.body request in
        Dream.set_session "user_id" "123" request;
        Dream.json {|{"token": "session"}|});
  ]
'''
    prompt = """\
You are the Route Extractor agent. Extract every HTTP endpoint from this route file into structured format.

## Code Observations
- Routing style: function-based routing
- Path parameter syntax: :param
- Base prefix: /api
- Request bodies: Dream.body
- Error handling: pattern matching on session

## Authentication
Endpoints with `Dream.session` checks use cookie/session-based auth.
Set security: [{"name": "CookieAuth", "scheme_type": "cookie"}] on endpoints that check sessions.
Endpoints without session checks (like login) are public → set security: [].

All field semantics are defined in the schema descriptions.
"""
    context = json.dumps({"framework": "dream", "base_path": "/api", "target_file": "server.ml"}, indent=2)
    user_msg = f"## Context\n\n```json\n{context}\n```\n\n## Route File: server.ml\n\n```ocaml\n{code}\n```"

    result = call_llm(prompt, user_msg, EndpointDescriptorSecTest)

    endpoints = {f"{e.method} {e.path}": e for e in result.endpoints}
    print(f"  Endpoints: {list(endpoints.keys())}")

    issues = []

    # Find the user endpoint (should have cookie auth)
    user_ep = None
    for key, ep in endpoints.items():
        if "user" in key.lower() and ep.method == "GET":
            user_ep = ep
            break

    if user_ep:
        if user_ep.security:
            scheme = user_ep.security[0]
            print(f"  GET /user security: name='{scheme.name}', scheme_type='{scheme.scheme_type}'")
            if scheme.scheme_type in ("cookie", "apikey"):
                print("    ✓ Correctly identified as cookie/apikey auth")
            elif scheme.scheme_type == "bearer":
                issues.append(f"scheme_type='bearer' — should be 'cookie' or 'apikey' for session auth")
        else:
            issues.append("GET /user has no security (should have cookie auth)")
    else:
        issues.append("GET /user endpoint not found")

    # Find login endpoint (should be public)
    login_ep = None
    for key, ep in endpoints.items():
        if "login" in key.lower() and ep.method == "POST":
            login_ep = ep
            break

    if login_ep:
        if login_ep.security:
            issues.append(f"POST /login has security (should be public): {login_ep.security[0].scheme_type}")
        else:
            print("  POST /login: security=[] (public) ✓")

    if issues:
        return False, "; ".join(issues)
    return True, "Cookie auth correctly typed"


# ============================================================================
# TEST 7: Optional auth detection (kotlin-ktor BUG)
# ============================================================================

def test_optional_auth_detection():
    """Test that authenticate(optional=true) is treated as public, not required auth."""
    code = '''\
package io.realworld.route

import io.ktor.server.application.*
import io.ktor.server.auth.*
import io.ktor.server.response.*
import io.ktor.server.routing.*

fun Routing.articles(articleController: ArticleController) {
    route("articles") {
        authenticate(optional = true) {
            get { articleController.getArticles(call) }       // GET /articles — public, optional auth
            get("{slug}") { articleController.getArticle(call) }  // GET /articles/{slug} — public
        }
        authenticate {
            post { articleController.createArticle(call) }     // POST /articles — requires auth
            route("{slug}") {
                put { articleController.updateArticle(call) }  // PUT /articles/{slug} — requires auth
                delete { articleController.deleteArticle(call) }  // DELETE /articles/{slug} — requires auth
            }
        }
    }
}
'''
    prompt = """\
You are the Route Extractor agent. Extract every HTTP endpoint from this route file into structured format.

## Code Observations
- Routing style: method chaining with nested route blocks
- Path parameter syntax: {param} (Ktor uses string interpolation)
- Base prefix: /articles
- Request bodies: call.receive
- Error handling: exceptions

## Authentication
- `authenticate { ... }` blocks require auth → set security: [{"name": "BearerAuth", "scheme_type": "bearer"}]
- `authenticate(optional = true) { ... }` blocks have OPTIONAL auth — the endpoint works without auth, auth is for personalization only → set security: [] (public)
- Endpoints outside any authenticate block are public → set security: []

All field semantics are defined in the schema descriptions.
"""
    context = json.dumps({"framework": "ktor", "base_path": "", "target_file": "src/route/Articles.kt"}, indent=2)
    user_msg = f"## Context\n\n```json\n{context}\n```\n\n## Route File: src/route/Articles.kt\n\n```kotlin\n{code}\n```"

    result = call_llm(prompt, user_msg, EndpointDescriptorSecTest)

    endpoints = {f"{e.method} {e.path}": e for e in result.endpoints}
    print(f"  Endpoints: {list(endpoints.keys())}")

    issues = []

    for key, ep in endpoints.items():
        has_auth = bool(ep.security)
        # GET endpoints should be public (optional auth)
        if ep.method == "GET":
            if has_auth:
                issues.append(f"{key} has auth but should be public (optional auth)")
            else:
                print(f"  {key}: security=[] (public) ✓")
        # POST/PUT/DELETE should require auth
        elif ep.method in ("POST", "PUT", "DELETE"):
            if not has_auth:
                issues.append(f"{key} has no auth but should require it")
            else:
                print(f"  {key}: security={ep.security[0].name} ✓")

    if issues:
        return False, "; ".join(issues)
    return True, "Optional auth correctly treated as public"


# ============================================================================
# TEST 8: Path parameter syntax conversion (current model, no change needed?)
# ============================================================================

def test_path_param_conversion():
    """Test that LLM converts framework path param syntax to OpenAPI {param} syntax."""
    code = '''\
const express = require('express');
const router = express.Router();

router.get('/users/:userId/posts/:postId', getPost);
router.put('/users/:userId/profile', updateProfile);
router.delete('/teams/:teamId/members/:memberId', removeMember);

module.exports = router;
'''
    prompt = """\
You are the Route Extractor agent. Extract every HTTP endpoint from this route file into structured format.

## Code Observations
- Routing style: method chaining
- Path parameter syntax: :param (Express.js)
- Base prefix: /api
- Request bodies: req.body
- Error handling: next(error)

## Authentication
No auth patterns detected. Set security: [] (public) on all endpoints.

## Strategy
- Combine base_path + router-level prefix + endpoint-level path. Convert path parameters to OpenAPI {param} syntax.
- Every path parameter segment in the URL must have a matching Parameter object.
"""
    context = json.dumps({"framework": "express", "base_path": "/api", "target_file": "routes/users.js"}, indent=2)
    user_msg = f"## Context\n\n```json\n{context}\n```\n\n## Route File: routes/users.js\n\n```javascript\n{code}\n```"

    result = call_llm(prompt, user_msg, EndpointDescriptorSecTest)

    endpoints = {f"{e.method} {e.path}": e for e in result.endpoints}
    print(f"  Endpoints: {list(endpoints.keys())}")

    issues = []
    for key in endpoints:
        if ":" in key:
            issues.append(f"Path still uses :param syntax: {key}")
        elif "<" in key:
            issues.append(f"Path uses <param> syntax: {key}")

    # Check that paths have {param} syntax
    found_curly = any("{" in key for key in endpoints)
    if not found_curly:
        issues.append("No paths with {param} syntax found")

    if issues:
        return False, "; ".join(issues)
    return True, f"All {len(endpoints)} paths use {{param}} syntax"


# ============================================================================
# TEST 9: mount_map extraction from registry files
# ============================================================================

from swagger_agent.models import CodeAnalysis

def test_mount_map_extraction():
    """Test that Phase 1 extracts mount_map from Express index.js."""
    code = '''\
const express = require('express');
const authRoute = require('./auth.route');
const userRoute = require('./user.route');
const docsRoute = require('./docs.route');

const router = express.Router();

const defaultRoutes = [
  { path: '/auth', route: authRoute },
  { path: '/users', route: userRoute },
  { path: '/docs', route: docsRoute },
];

defaultRoutes.forEach((route) => {
  router.use(route.path, route.route);
});

module.exports = router;
'''
    from swagger_agent.agents.route_extractor.prompt import CODE_ANALYSIS_PROMPT
    context = json.dumps({"framework": "express", "base_path": "/v1", "target_file": "src/routes/v1/index.js"}, indent=2)
    user_msg = f"## Context\n\n```json\n{context}\n```\n\n## Route File: src/routes/v1/index.js\n\n```javascript\n{code}\n```"

    result = call_llm(CODE_ANALYSIS_PROMPT, user_msg, CodeAnalysis)

    print(f"  mount_map: {result.mount_map}")
    print(f"  endpoints: {[(e.method, e.path) for e in result.endpoints]}")
    print(f"  routing_style: {result.routing_style}")

    # mount_map should map sub-files to their mount paths
    if not result.mount_map:
        return False, "mount_map is empty — LLM didn't extract sub-router mounts"

    # Check that user route is in mount_map
    has_user_mount = any("user" in k.lower() and "/users" in v for k, v in result.mount_map.items())
    has_auth_mount = any("auth" in k.lower() and "/auth" in v for k, v in result.mount_map.items())

    print(f"  Has user mount: {has_user_mount}")
    print(f"  Has auth mount: {has_auth_mount}")

    if not has_user_mount:
        return False, f"mount_map missing user route: {result.mount_map}"
    if not has_auth_mount:
        return False, f"mount_map missing auth route: {result.mount_map}"

    return True, f"mount_map has {len(result.mount_map)} entries with correct prefixes"


# ============================================================================
# TEST 10: Haskell/unusual language schema extraction
# ============================================================================

def test_haskell_schema_extraction():
    """Test Schema Extractor on Haskell record types (non-TH, plain records)."""
    code = '''\
{-# LANGUAGE DeriveGeneric #-}

module Types where

import GHC.Generics
import Data.Aeson

data User = User
    { userId      :: Int
    , userName    :: String
    , userEmail   :: String
    , userBio     :: Maybe String
    , userImage   :: Maybe String
    } deriving (Show, Generic)

instance ToJSON User
instance FromJSON User

data CreateUserRequest = CreateUserRequest
    { newUserEmail    :: String
    , newUserPassword :: String
    , newUserName     :: String
    } deriving (Show, Generic)

instance ToJSON CreateUserRequest
instance FromJSON CreateUserRequest
'''
    context = json.dumps({
        "framework": "servant",
        "target_file": "src/Types.hs",
        "known_schemas": {},
    }, indent=2)
    user_msg = f"## Context\n\n```json\n{context}\n```\n\n## Model File: src/Types.hs\n\n```haskell\n{code}\n```"

    result = call_llm(SCHEMA_PROMPT, user_msg, SchemaDescriptorBasic)

    schemas = {s.name: s for s in result.schemas}
    print(f"  Schemas found: {list(schemas.keys())}")

    issues = []
    for name, expected_min in [("User", 4), ("CreateUserRequest", 3)]:
        if name not in schemas:
            issues.append(f"{name} not found")
            continue
        props = [p.name for p in schemas[name].properties]
        print(f"  {name} properties ({len(props)}): {props}")
        if len(props) < expected_min:
            issues.append(f"{name} has {len(props)} properties, expected {expected_min}+")

    if issues:
        return False, "; ".join(issues)
    return True, f"User has {len(schemas['User'].properties)} props, CreateUserRequest has {len(schemas['CreateUserRequest'].properties)} props"


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    print(f"LLM: {config.llm_base_url} / {config.llm_model}")
    print(f"Mode: {config.instructor_mode}")
    print(f"Max tokens: {config.llm_max_tokens}")
    print(f"Temperature: {config.llm_temperature}")

    tests = [
        ("1. SchemaProperty constraints (Python/Pydantic)", test_constraints_extraction_python),
        ("2. SchemaProperty constraints (Kotlin annotations)", test_constraints_extraction_kotlin),
        ("3. Go struct extraction (BUG 7)", test_go_struct_extraction),
        ("4. Kotlin data class extraction", test_kotlin_data_class_extraction),
        ("5. Clojure defschema extraction", test_clojure_schema_extraction),
        ("6. RefHint is_array usage", test_refhint_is_array),
        ("7. Cookie auth scheme type", test_cookie_auth_scheme_type),
        ("8. Optional auth detection", test_optional_auth_detection),
        ("9. Path param syntax conversion", test_path_param_conversion),
        ("10. mount_map from registry file", test_mount_map_extraction),
        ("11. Haskell record extraction", test_haskell_schema_extraction),
    ]

    for name, fn in tests:
        run_test(name, fn)

    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    passed = sum(1 for _, p, _ in results if p)
    total = len(results)
    for name, p, details in results:
        status = PASS if p else FAIL
        print(f"  {status} {name}: {details}")
    print(f"\n  {passed}/{total} tests passed")
