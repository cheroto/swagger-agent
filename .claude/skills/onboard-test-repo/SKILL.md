---
name: onboard-test-repo
description: Find, clone, and onboard new test repositories for the swagger-agent e2e test suite. Analyzes coverage gaps, searches GitHub for candidate repos, clones them, guides manual golden data curation by reading route/model files, and generates e2e test entries.
argument-hint: "[github-url-or-search-query]"
disable-model-invocation: true
---

# Onboard Test Repository

You are onboarding a new test repository for the swagger-agent e2e test suite.

**IMPORTANT**: Golden data must be MANUALLY curated by reading the actual source code. Never guess or infer golden values — read the route/model files and report exactly what's there. Ask the user to confirm every golden data entry before writing test code.

## Context

The swagger-agent tests live in `tests/e2e/`. Key files:
- `repos.json` — manifest of all test repos (git URL + pinned commit)
- `test_route_extractor.py` — route extraction golden data and tests
- `test_schema_loop.py` — schema extraction golden data and tests
- `helpers.py` — assertion helpers and golden data types
- `repos/` — cloned repos (gitignored, cloned on demand by conftest)

## Current Coverage Matrix

Read `tests/e2e/repos.json`, `tests/e2e/test_route_extractor.py`, and `tests/e2e/test_schema_loop.py` to build the current coverage matrix. Track these dimensions:

| Dimension | Currently Covered |
|-----------|-------------------|
| **Languages** | JavaScript, Java, PHP, C#, Rust, Python, Go, TypeScript |
| **Frameworks** | Express, Spring Boot, Laravel, ASP.NET Core, Axum, FastAPI, Go net/http, AWS Lambda |
| **Auth patterns** | Middleware chain, decorator/annotation, inference from code, none |
| **Resolution** | `import` (JS require, Python from/import, Java import), `class_to_file` (Rust struct, Go struct) |
| **Model styles** | Mongoose schema, SQLAlchemy, JPA @Entity, Rust struct, Go struct |
| **Path syntax** | `{param}`, `:param` |
| **Request body** | JSON, multipart/form-data |

### Coverage Gaps to Prioritize

1. **Flask/Django** (Python) — `<param>` path syntax, decorator-based routing
2. **NestJS** (TypeScript) — class-based controllers, `@UseGuards`, class-validator DTOs
3. **Ruby on Rails** — `resources` macro, convention-over-config routing
4. **Gin/Echo/Fiber** (Go) — framework-level routing vs stdlib
5. **cookie/session auth** — no test case exists
6. **form-urlencoded bodies** — no test case exists
7. **TypeScript interfaces as models** — only Go/Rust structs tested for `class_to_file`

---

## Phase 1: Identify Target

If the user provided a GitHub URL as `$ARGUMENTS`, skip to Phase 2.

If the user provided a search query or no arguments:

1. Show the coverage matrix above (populated from current test files)
2. Identify the top 3 coverage gaps
3. Search GitHub for small, well-structured REST API repos that fill these gaps

**Search criteria for good test repos:**
- Small codebase (ideally < 50 files, definitely < 200)
- Clear route definitions (not hidden behind code generation)
- Has model/schema definitions (not just controllers)
- Uses a mainstream framework
- Has at least 3-5 endpoints with a mix of HTTP methods
- Ideally has auth on some endpoints
- Public repo, permissive license preferred

Use `gh search repos` or WebSearch to find candidates. Present 3-5 options to the user with:
- Repo URL
- Framework / language
- What coverage gap it fills
- Approximate size (files, stars)

Wait for the user to pick one before proceeding.

---

## Phase 2: Clone and Inspect

1. Clone the repo into `tests/e2e/repos/<repo-name>/`:
   ```bash
   git clone <url> tests/e2e/repos/<repo-name>
   ```

2. Pin the commit:
   ```bash
   git -C tests/e2e/repos/<repo-name> rev-parse HEAD
   ```

3. Inspect the codebase — find and read:
   - **Route files**: Look for controllers, route definitions, handler registrations
   - **Model files**: Look for schema/model/entity/DTO definitions
   - **Auth setup**: Middleware, guards, decorators, config files
   - **Import patterns**: How are models imported in route files?

4. Present a summary to the user:
   ```
   Repo: <name>
   Framework: <framework>
   Language: <language>

   Route files found:
     - path/to/routes.py (N endpoints)
     - path/to/other.py (M endpoints)

   Model files found:
     - path/to/models.py (classes: User, Post, Comment)

   Auth pattern: <description>
   Path param syntax: <syntax>
   ```

Wait for the user to confirm which route file to use for golden data.

---

## Phase 3: Curate Route Extraction Golden Data

For the chosen route file, read it completely and extract golden data BY HAND:

### 3a. Phase 1 (Code Analysis) Golden

Read the file and identify:
- **Endpoints**: List every `(method, path, handler_name)` tuple you see in the code
- **Auth patterns**: What auth mechanism is used? (middleware, decorator, annotation, none)
- **Auth imports**: Are there auth-related imports?
- **Base prefix**: Is there a router-level prefix?
- **Path param syntax**: What style? (`:param`, `{param}`, `<param>`)
- **Import lines**: What imports contain type names used in the endpoints?

Present this to the user in a structured format and ask them to verify/correct.

### 3b. Phase 2 (Endpoint Extraction) Golden

For each endpoint, identify:
- **Method**: GET/POST/PUT/PATCH/DELETE
- **Full path**: Including base path and router prefix
- **Auth**: Does this specific endpoint require auth? (`True`/`False`/`None` for don't-check)
- **Request body**: Does it accept a body? (`True`/`False`/`None`)
- **Path params**: List parameter names that appear in the URL
- **Min responses**: How many response codes should we expect? (usually 1-2)

Present this as a table and ask the user to verify/correct.

### 3c. Write the golden data

Only after user confirmation, construct the `RouteGolden` entry. Use the exact format from `test_route_extractor.py`:

```python
RouteGolden(
    repo_id="<repo-name>",
    repo_dir="<repo-name>",
    route_file="<relative-path-to-route-file>",
    framework="<framework>",
    base_path="<base-path>",
    min_endpoints=N,
    endpoints=[
        ExpectedEndpoint(
            method="GET",
            path="/api/users/{id}",
            has_auth=True,
            has_request_body=False,
            param_names=["id"],
            min_responses=2,
        ),
        # ... more endpoints
    ],
    phase1=Phase1Golden(
        min_endpoints=N,
        endpoints=[
            ExpectedPhase1Endpoint(method="GET", path="/api/users/{id}", handler_name="get_user"),
            # ... more sketches
        ],
        has_auth_patterns=True,
        has_auth_imports=True,
        base_prefix="/api",
        path_param_syntax="{",
        required_import_substrings=["models"],
    ),
),
```

---

## Phase 4: Curate Schema Extraction Golden Data

### 4a. Identify ref_hints

From the route file's imports and type annotations, identify which types would become ref_hints:
- Request body types (e.g., `CreateUserRequest`)
- Response types (e.g., `UserResponse`)

For each, determine:
- `ref_hint`: The type name as it appears in code
- `import_source`: The exact import line (or `null` if same-file/implicit)
- `resolution`: `"import"` if imported, `"class_to_file"` if same-package, `"unresolvable"` if external

### 4b. Read model files

For each resolvable ref_hint, find and read the model file. Extract:
- Schema name (class/struct/interface name)
- Properties with their types
- Required fields
- Which properties are essential to assert (pick 2-4 distinctive ones)

### 4c. Write the golden data

Construct the `SchemaLoopGolden` entry:

```python
SchemaLoopGolden(
    repo_id="<repo-name>",
    repo_dir="<repo-name>",
    framework="<framework>",
    ref_hints=[
        {
            "ref_hint": "User",
            "import_source": "from app.models import User",
            "resolution": "import",
        },
    ],
    min_schemas=N,
    expected_schemas=[
        ExpectedSchema(
            name="User",
            min_properties=3,
            expected_properties=["id", "username", "email"],
        ),
    ],
),
```

---

## Phase 5: Update Test Files

After user confirms all golden data:

1. **Update `repos.json`** — add the new repo entry:
   ```json
   "<repo-name>": {
     "url": "<git-url>",
     "commit": "<pinned-commit-hash>",
     "framework": "<framework>",
     "language": "<language>"
   }
   ```

2. **Update `test_route_extractor.py`** — append the new `RouteGolden` to `ROUTE_GOLDEN` list

3. **Update `test_schema_loop.py`** — append the new `SchemaLoopGolden` to `SCHEMA_GOLDEN` list (if the repo has resolvable schemas)

4. **Run the new test** to verify:
   ```bash
   pytest tests/e2e/test_route_extractor.py::test_route_extraction[<repo-name>] -m e2e -v
   pytest tests/e2e/test_schema_loop.py::test_schema_loop[<repo-name>] -m e2e -v
   ```

5. If the test fails, analyze the output:
   - If it's a golden data issue (wrong path, wrong auth expectation), fix the golden
   - If it's an infrastructure issue (resolution failure, wrong file), investigate `resolve.py`
   - If it's an LLM output issue, consider whether the golden expectation is too strict

6. Run the full suite to confirm no regressions:
   ```bash
   pytest tests/e2e/ -m e2e -v
   ```

---

## Checklist Before Done

- [ ] Repo cloned and commit pinned in `repos.json`
- [ ] Route file identified and golden data confirmed by user
- [ ] Phase 1 golden data includes: min_endpoints, endpoint sketches, auth patterns, imports
- [ ] Phase 2 golden data includes: endpoints with method, path, auth, body, params
- [ ] Schema golden data includes: ref_hints with resolution strategy, expected schemas
- [ ] New route extraction test passes
- [ ] New schema loop test passes (if applicable)
- [ ] Full test suite passes (no regressions)
