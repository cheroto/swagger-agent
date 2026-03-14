# E2E Route Extractor Test Findings

## 1. rest-api-node (5 test cases)

| Test ID | Result | Details |
|---------|--------|---------|
| rest-api-node | PASS | Private user routes (PUT/DELETE) — auth, params, body all correct |
| rest-api-node-private-project | PASS | Private project routes (POST/PUT/DELETE) — all correct |
| rest-api-node-public-project | PASS | Public project routes (GET list/by-id) — all correct |
| rest-api-node-public-user | PASS | Public user routes (POST/GET/GET-by-id) — all correct |
| rest-api-node-service | **FAIL** | Health check routes — 0 endpoints extracted, expected 2 |

### rest-api-node-service failure analysis

**Symptom:** Extracted 0 endpoints from `src/routes/public/service.js`. Expected GET /liveness_check and GET /readiness_check.

**Root cause: Solution bug (ctags prefilter).** The log shows: `Ctags prefilter: unmatched handlers in .../service.js: ['anonymous', 'anonymous']`. The service file uses anonymous inline handlers (not named controller functions), so ctags tags them as "anonymous". The prefilter can't match these anonymous handlers to route registrations and apparently drops them, resulting in no code being sent to the LLM for extraction. This is a **solution problem** — the prefilter is too aggressive when handlers are anonymous/inline functions.

---

## 2. spring-boot-blog (1 test case)

| Test ID | Result | Details |
|---------|--------|---------|
| spring-boot-blog | PASS | PostController (7 endpoints) — all methods, paths, auth, params correct |

---

## 3. laravel-realworld (1 test case)

| Test ID | Result | Details |
|---------|--------|---------|
| laravel-realworld | **FAIL** | POST /api/users/login missing request body |

### laravel-realworld failure analysis

**Symptom:** Endpoints were found (passed min_endpoints=15 check), but POST /api/users/login has no `request_body`.

**Root cause: LLM extraction quality.** The ctags prefilter couldn't reduce this file (PHP route file with `Controller@method` string references produces no ctags), so the full file was sent to the LLM. The LLM (Qwen 3.5 35B) extracted the endpoints but missed the request body for the login endpoint. Laravel routes/api.php only declares routes with controller references — the actual request body is in the controller, not the route file. This is a **borderline case**: the golden data expects the LLM to infer that a login POST needs a request body even though the route file doesn't show the body shape. Could argue **golden data is too strict** (route file genuinely doesn't show the body) or **LLM should infer** POST login implies credentials body.

---

## 4. aspnetcore-realworld (1 test case)

| Test ID | Result | Details |
|---------|--------|---------|
| aspnetcore-realworld | **FAIL** | 0 endpoints extracted, expected 6 |

### aspnetcore-realworld failure analysis

**Symptom:** Extracted 0 endpoints from `src/Conduit/Features/Articles/ArticlesController.cs`.

**Root cause: Solution bug (ctags prefilter).** Log: `Ctags prefilter: unmatched handlers in .../ArticlesController.cs: ['Get', 'GetFeed', 'Get', 'Create', 'Edit', 'Delete']`. Ctags found the handler methods but the prefilter couldn't match them to route registrations (ASP.NET uses attribute-based routing like `[HttpGet]`, not explicit route registration calls). The prefilter drops all code, leaving nothing for the LLM. Same class of bug as rest-api-node-service — **prefilter is too aggressive**, failing to handle attribute-routed controllers.

---

## 5. passwordless-auth-rust (1 test case)

| Test ID | Result | Details |
|---------|--------|---------|
| passwordless-auth-rust | PASS | All 9 Axum endpoints extracted correctly |

---

## 6. levo-schema-service (1 test case)

| Test ID | Result | Details |
|---------|--------|---------|
| levo-schema-service | **FAIL** | POST /schemas/import missing request body |

### levo-schema-service failure analysis

**Symptom:** All 4 endpoints found, but POST /schemas/import has no `request_body`.

**Root cause: LLM extraction quality.** The FastAPI route file has `UploadFile` parameters on the import endpoint — the LLM should have recognized this as a multipart/form-data request body but didn't produce a `request_body` object. This is a **solution/LLM problem** — the code clearly shows a file upload parameter, the LLM failed to map it to a request body.

---

## 7. 9jauni (1 test case)

| Test ID | Result | Details |
|---------|--------|---------|
| 9jauni | **FAIL** | GET /searchab missing query param 'abbreviation' |

### 9jauni failure analysis

**Symptom:** All 3 endpoints found, but GET /searchab is missing the `abbreviation` query parameter.

**Root cause: LLM extraction quality.** The Go handler reads `r.URL.Query().Get("abbreviation")` — the LLM found the endpoint but missed extracting this query parameter. Also ctags prefilter warning: `unmatched handlers: ['getAllUni', 'getAUni', 'getAUniByAB']` — but the endpoints were still extracted (prefilter didn't block here, just warned). This is an **LLM problem** — the parameter is clearly in the code.

---

## 8. energy-monitoring-app (1 test case)

| Test ID | Result | Details |
|---------|--------|---------|
| energy-monitoring-app | **FAIL** | Phase 1 path mismatch: LLM produced `/energy/get-history`, golden expects `/energy/history` |

### energy-monitoring-app failure analysis

**Symptom:** Phase 1 sketch has path `/energy/get-history` but golden expects `/energy/history`.

**Root cause: Golden data problem.** This is an AWS Lambda handler — the file is `src/handlers/energy/get-history.ts`. There are no route decorators; routes come from infra config (SAM/CloudFormation). The LLM inferred the path from the file path (`get-history.ts` → `/energy/get-history`), which is a reasonable guess. The golden data says `/energy/history` which is also a guess. Neither can be verified from the handler code alone. **Golden data should match what the LLM can reasonably infer** from the file path, or both should be considered acceptable.

---

## 9. dotnet-clean-architecture-todoitems (1 test case)

| Test ID | Result | Details |
|---------|--------|---------|
| dotnet-clean-architecture-todoitems | PASS | All 5 Minimal API endpoints correct |

---

## 10. dotnet-clean-architecture-todolists (1 test case)

| Test ID | Result | Details |
|---------|--------|---------|
| dotnet-clean-architecture-todolists | PASS | All 4 Minimal API endpoints correct |

---

## 11. dotnet-bitwarden-folders (1 test case)

| Test ID | Result | Details |
|---------|--------|---------|
| dotnet-bitwarden-folders | PASS | All 6+ endpoints extracted with auth, params correct |

---

## 12. dotnet-bitwarden-securitytask (1 test case)

| Test ID | Result | Details |
|---------|--------|---------|
| dotnet-bitwarden-securitytask | **FAIL** | Phase 1 base_prefix empty, expected `/tasks` |

### dotnet-bitwarden-securitytask failure analysis

**Symptom:** Phase 1 analysis has `base_prefix=""` but golden expects `/tasks`. The LLM found all 5 endpoints and their paths include `/tasks/...`, but it reported the base prefix as empty string.

**Root cause: LLM extraction quality.** The controller has `[Route("tasks")]` attribute — the LLM should have extracted this as the base prefix. It correctly used it in endpoint paths (e.g. `/tasks/{taskId}/complete`) but didn't report it in the `base_prefix` field. Also note ctags prefilter warning: `unmatched handlers: ['Get']` — one of the overloaded Get methods wasn't matched, but endpoints were still extracted. This is an **LLM problem** — it sees the `[Route("tasks")]` but fails to populate `base_prefix`.

---

## 13. dotnet-bitwarden-sync (1 test case)

| Test ID | Result | Details |
|---------|--------|---------|
| dotnet-bitwarden-sync | PASS | Single GET /sync endpoint with auth and query param correct |

---

## Summary

| # | Repo | Tests | Passed | Failed | Failure Category |
|---|------|-------|--------|--------|-----------------|
| 1 | rest-api-node | 5 | 4 | 1 | Solution bug (ctags prefilter) |
| 2 | spring-boot-blog | 1 | 1 | 0 | — |
| 3 | laravel-realworld | 1 | 0 | 1 | LLM quality / golden data borderline |
| 4 | aspnetcore-realworld | 1 | 0 | 1 | Solution bug (ctags prefilter) |
| 5 | passwordless-auth-rust | 1 | 1 | 0 | — |
| 6 | levo-schema-service | 1 | 0 | 1 | LLM quality |
| 7 | 9jauni | 1 | 0 | 1 | LLM quality |
| 8 | energy-monitoring-app | 1 | 0 | 1 | Golden data problem |
| 9 | dotnet-clean-architecture-todoitems | 1 | 1 | 0 | — |
| 10 | dotnet-clean-architecture-todolists | 1 | 1 | 0 | — |
| 11 | dotnet-bitwarden-folders | 1 | 1 | 0 | — |
| 12 | dotnet-bitwarden-securitytask | 1 | 0 | 1 | LLM quality |
| 13 | dotnet-bitwarden-sync | 1 | 1 | 0 | — |
| **TOTAL** | | **17** | **10** | **7** | |

### Failure breakdown by root cause

**Solution bugs (ctags prefilter) — 2 failures:**
- rest-api-node-service: anonymous inline handlers tagged as "anonymous", prefilter drops all code
- aspnetcore-realworld: attribute-routed controller methods unmatched by prefilter, 0 endpoints extracted

**LLM extraction quality — 4 failures:**
- laravel-realworld: POST /api/users/login missing request body (borderline — route file doesn't show body)
- levo-schema-service: POST /schemas/import missing request body (UploadFile clearly in code)
- 9jauni: GET /searchab missing query param `abbreviation` (clearly in code via `r.URL.Query().Get`)
- dotnet-bitwarden-securitytask: base_prefix empty despite `[Route("tasks")]` attribute present

**Golden data problem — 1 failure:**
- energy-monitoring-app: LLM inferred `/energy/get-history` from filename, golden says `/energy/history` — neither verifiable from code, both are guesses

---

# E2E Schema Loop Test Findings

## 1. rest-api-node

| Test ID | Result | Details |
|---------|--------|---------|
| rest-api-node | PASS | User and Project Mongoose schemas extracted correctly |

---

## 2. levo-schema-service

| Test ID | Result | Details |
|---------|--------|---------|
| levo-schema-service | PASS | Application and Schema SQLAlchemy models extracted correctly |

---

## 3. passwordless-auth-rust

| Test ID | Result | Details |
|---------|--------|---------|
| passwordless-auth-rust | PASS | Rust structs (RequestMagicBody, AuthResponse, TotpVerifyBody, RefreshBody) extracted correctly |

---

## 4. 9jauni

| Test ID | Result | Details |
|---------|--------|---------|
| 9jauni | PASS | Go structs (uniRequest, errorResponse) extracted correctly |

---

## 5. spring-boot-blog

| Test ID | Result | Details |
|---------|--------|---------|
| spring-boot-blog | PASS | Java Post model and PostRequest extracted correctly |

---

## 6. aspnetcore-realworld

| Test ID | Result | Details |
|---------|--------|---------|
| aspnetcore-realworld | PASS | C# envelope types (ArticleEnvelope, ArticlesEnvelope, TagsEnvelope, ProfileEnvelope, UserEnvelope) extracted correctly |

---

## Schema Loop Summary

| # | Repo | Result |
|---|------|--------|
| 1 | rest-api-node | PASS |
| 2 | levo-schema-service | PASS |
| 3 | passwordless-auth-rust | PASS |
| 4 | 9jauni | PASS |
| 5 | spring-boot-blog | PASS |
| 6 | aspnetcore-realworld | PASS |
| **TOTAL** | **6/6** | **All passing** |

---

# Full Pipeline Run: aspnetcore-realworld

## Results

**Pipeline output:** 2 endpoints extracted out of ~20+ actual endpoints across 8 controllers. Catastrophic failure.

| Controller | Handlers Found by Ctags | Endpoints Extracted | Prefilter Outcome |
|-----------|------------------------|--------------------|--------------------|
| ArticlesController.cs | Get, GetFeed, Get, Create, Edit, Delete | 0 | **All unmatched** |
| TagsController.cs | Get | 0 | **All unmatched** |
| UserController.cs | GetCurrent, UpdateUser | 2 | **Extracted** (only success) |
| UsersController.cs | Create, Login | 0 | **All unmatched** |
| CommentsController.cs | Create, Get, Delete | 0 | **All unmatched** |
| FavoritesController.cs | FavoriteAdd, FavoriteDelete | 0 | **All unmatched** |
| FollowersController.cs | Follow, Unfollow | 0 | **All unmatched** |
| ProfilesController.cs | Get | 0 | **All unmatched** |

**Completeness:** has_endpoints=True, has_schemas=False, route_coverage=1.0 (misleading — it processed all files but extracted almost nothing)

## Root Cause

**The ctags prefilter is systematically failing on ASP.NET attribute-routed controllers.** It finds handler method names via ctags but cannot match them to route registrations because ASP.NET uses `[HttpGet]`, `[HttpPost]` etc. attributes — not explicit route registration calls like `app.get("/path", handler)`. When handlers go unmatched, the prefilter drops all code, sending nothing to the LLM.

UserController.cs succeeded likely because its method names or structure happened to pass the prefilter (or the file was small enough to skip prefiltering).

## Why This Isn't Caught by E2E Tests

The existing e2e test structure has a **coverage gap** that hides this systemic failure:

1. **Route extractor e2e tests test individual files in isolation.** The `test_route_extractor.py` test for `aspnetcore-realworld` tests only `ArticlesController.cs` — and it already fails (0 endpoints). But it's just **1 failing test case** among 17, easy to overlook.

2. **There is no pipeline e2e test for aspnetcore-realworld.** The `test_pipeline.py` has golden data for `rest-api-node`, `dotnet-clean-architecture`, and `dotnet-bitwarden` — but **not** for `aspnetcore-realworld`. So the cascading effect (all 8 controllers failing → 2/20+ endpoints → useless spec) is never tested end-to-end.

3. **Schema loop tests for aspnetcore-realworld pass (6/6)** because they test ref resolution independently with curated ref_hints — they don't depend on route extraction succeeding first.

4. **The single route extractor failure doesn't convey severity.** One test failing for `aspnetcore-realworld` looks like a minor issue. But when 7 out of 8 controllers in the same repo all fail the same way, it's a systemic prefilter bug affecting an entire framework pattern. The per-file test granularity masks the per-repo impact.

5. **Route coverage metric is misleading.** `route_coverage: 1.0` because all 8 files were *attempted*. The metric tracks files processed, not endpoints successfully extracted. A coverage metric based on expected-vs-actual endpoint count would catch this.

---

# Full Pipeline Runs: All Smaller Repos

## 1. rest-api-node

**Result: 11 endpoints, 2 schemas — mostly good**

| Metric | Value | Notes |
|--------|-------|-------|
| Route files found | 5 | Includes express.js config (extracted GET /docs from it — extra) |
| Endpoints extracted | 11 | Expected ~12 (missing service health routes — prefilter bug) |
| Schemas | 2 (User, Project) | Correct |
| Validation errors | 1 | Project schema has `"required": []` — empty array not valid per OpenAPI spec |
| Security schemes | BearerAuth | Correct |
| Servers | http://localhost:3000 | Correct |

**Issues found:**
- **Missing service routes** (GET /liveness_check, GET /readiness_check) — same ctags prefilter bug with anonymous handlers. The `src/routes/public/service.js` file wasn't even in the route list (Scout found config/express.js instead).
- **GET /docs extracted from express.js** — this is the Swagger UI route from the config file, not a real API endpoint. Scout shouldn't have listed express.js as a route file.
- **1 validation error:** Project schema has `"required": []` — OpenAPI 3.0 requires `required` array to be non-empty if present. LLM produced an empty array instead of omitting the field.
- **endpoints_have_auth: FAIL** — public endpoints correctly have no auth, but the completeness checker flags them as missing security declarations (it wants explicit `security: []`).
- **has_error_responses: FAIL** — public endpoints don't have error responses.

**Why not caught by pipeline e2e:** There IS a pipeline test for rest-api-node. It expects min_endpoints=10 (got 11, passes), but expects GET /liveness_check and GET /readiness_check which would fail. The `max_validation_errors=0` would also fail with the 1 validation error. **This pipeline test should be failing** — need to verify.

---

## 2. spring-boot-blog

**Result: 54 endpoints, 28 schemas — excellent**

| Metric | Value | Notes |
|--------|-------|-------|
| Route files found | 9 | All controllers discovered |
| Endpoints extracted | 54 | Comprehensive extraction |
| Schemas | 28 (2 rounds) | Full resolution including transitive deps (Photo discovered in round 2) |
| Validation errors | 1 | Address schema has `"required": []` — same empty-array bug as rest-api-node |
| Security schemes | BearerAuth | Correct |
| Servers | http://localhost:8080 | Correct |

**Issues found:**
- **1 validation error:** Same `"required": []` empty array bug on Address schema.
- **has_error_responses: FAIL** — endpoints don't include error responses (401/403/404).
- **has_request_bodies: FAIL** — PUT /completeTodo, PUT /unCompleteTodo, PUT /giveAdmin, PUT /takeAdmin are PUT without request bodies (correctly — these are state-toggle endpoints). The completeness checker incorrectly flags PUT endpoints that legitimately have no body.
- **POST /api/auth/signin has auth** — this is the login endpoint, it should be public. LLM incorrectly assigned BearerAuth to it.
- **POST /api/users has auth** — this appears to be user creation, should likely be admin-only or public depending on context. Borderline.

**Why not caught by pipeline e2e:** No pipeline test exists for spring-boot-blog. Only a single route extractor test (PostController) which passes.

---

## 3. laravel-realworld

**Result: 22 endpoints, 0 schemas — endpoints found but quality is poor**

| Metric | Value | Notes |
|--------|-------|-------|
| Route files found | 2 | routes/api.php + routes/web.php |
| Endpoints extracted | 22 | Good count (21 from api.php + 1 from web.php) |
| Schemas | 0 | No ref_hints produced → no schema resolution |
| Validation errors | 0 | Structurally valid but empty |
| Security schemes | None | Not detected |
| Servers | localhost:8000, localhost | Correct |

**Issues found:**
- **No auth on any endpoint.** All endpoints have `security: []` (empty). Laravel uses middleware groups (`auth.api`) defined in the route file — the LLM saw the middleware declarations but didn't map them to security schemes. No securitySchemes in the spec at all.
- **No request bodies on any POST/PUT/PATCH.** 11 warnings for missing request bodies. The route file only references `Controller@method` strings — actual request body info is in the controllers, not in routes/api.php. The LLM couldn't infer bodies from route declarations alone.
- **No schemas.** Route extractor produced no ref_hints because the route file has no type imports — it only has controller references. Schema resolution never triggered.
- **Missing /api prefix.** All paths should be under /api/ (Laravel routes/api.php auto-prefixes with /api), but the extracted paths have no prefix. The LLM didn't know about Laravel's automatic /api prefix convention.
- **Duplicate endpoints with PATCH.** PUT /articles/{article} and PATCH /articles/{article} both appear — the route file likely has both, but the golden data only expected PUT.
- **GET / from web.php.** The welcome route was extracted — technically correct but not an API endpoint.

**Why not caught by pipeline e2e:** No pipeline test exists for laravel-realworld. The single route extractor test already fails (missing request body on login).

---

## 4. passwordless-auth-rust

**Result: 30 endpoints, 20 schemas — but heavy duplication**

| Metric | Value | Notes |
|--------|-------|-------|
| Route files found | 14 | Scout over-identified: included db.rs, email_queue.rs, audit.rs, middleware.rs, etc. as route files |
| Endpoints extracted | 30 | But many are duplicates |
| Schemas | 20 | Good — all resolved including admin/metrics types |
| Validation errors | 0 | Clean |
| Security schemes | None | Not detected — this repo has no auth on its endpoints (it *provides* auth) |
| Servers | http://localhost:8080 | Correct |

**Issues found:**
- **Massive endpoint duplication.** The real API has 9 core auth endpoints. The pipeline found 30 because:
  - `src/routes.rs` extracted 9 endpoints (the canonical ones with `/request/magic` style paths)
  - `axum/mod.rs` should have matched the same 9 but couldn't (ctags prefilter: unmatched handlers with `handlers::` prefix)
  - `axum/handlers.rs` extracted 9 more endpoints but with different path format (`/request-magic` vs `/request/magic`)
  - `src/admin.rs`, `src/session.rs`, `src/webhooks.rs`, `src/metrics.rs` extracted admin/internal endpoints the Scout shouldn't have listed as route files
- **Scout over-discovery.** 14 route files is way too many for a 9-endpoint API. Scout included utility modules (db, email_queue, audit, middleware, magic_link, session, webauthn, webhooks) that aren't route files.
- **Duplicate paths with different naming:** `/request-magic` vs `/request/magic`, `/totp-enroll` vs `/totp/enroll`, etc. The handlers.rs file has the handler functions; routes.rs has the route definitions. Both were extracted producing duplicates with slightly different paths.
- **has_security_schemes: FAIL** — correct behavior (this API provides auth, doesn't require it), but the completeness checker flags it.

**Why not caught by pipeline e2e:** No pipeline test exists for this repo.

---

## 5. levo-schema-service

**Result: 4 endpoints, 0 schemas — endpoints correct but missing schemas and request body**

| Metric | Value | Notes |
|--------|-------|-------|
| Route files found | 1 | Correct (Code/app/routes.py) |
| Endpoints extracted | 4 | Correct count and paths |
| Schemas | 0 | No ref_hints → no resolution |
| Validation errors | 0 | Clean |
| Security schemes | None | Correct (no auth in this API) |
| Servers | http://127.0.0.1:9000/api | Correct |

**Issues found:**
- **POST /schemas/import missing request body.** The endpoint uses `UploadFile` (multipart/form-data) but the LLM didn't produce a requestBody. It did extract query params (application, service, replace, file) — the `file` param should be the upload body, not a query param. Same issue as the route extractor e2e test.
- **No schemas resolved.** The route extractor produced no ref_hints (no type imports for response models), so schema resolution never triggered. The actual code returns SQLAlchemy model objects — the LLM should have created ref_hints for Application, Service, Schema models.
- **has_request_bodies: FAIL** — POST import has no requestBody.
- **has_schemas: FAIL** — no schemas in the spec.

**Why not caught by pipeline e2e:** No pipeline test exists for this repo.

---

## Remaining smaller repos (not run — stopped per user request)

- 9jauni
- energy-monitoring-app

---

# Polymorphism / Discriminated Union Test Runs

Two repos specifically chosen to test ctags inheritance discovery and oneOf/discriminator assembly.

## 14. dograh (Python/FastAPI — discriminated unions)

**Target polymorphism:** `ToolDefinition = Annotated[Union[HttpApiToolDefinition, EndCallToolDefinition, TransferCallToolDefinition], Field(discriminator="type")]` in `api/routes/tool.py`. Each subtype has a different config shape (`HttpApiConfig`, `EndCallConfig`, `TransferCallConfig`).

**Pipeline result:** 60 endpoints, 30 schemas, 1 validation error, 19 warnings

| Metric | Value | Notes |
|--------|-------|-------|
| Route files found | 8 / 24 | Scout missed 16 route files including tool.py |
| Endpoints extracted | 60 | From 8 files only |
| Schemas | 30 | Resolved from ref_hints |
| Validation errors | 1 | `ge`/`le` instead of `minimum`/`maximum` |
| Unresolved schemas | 2 | `str, Any` and `List[TestSessionResponse]` |
| Security schemes | BearerAuth | Correct |
| Servers | app.dograh.com, localhost:8000 | Correct |
| Total time | 395s | |

### Issues found

#### ISSUE D1: Scout missed 16/24 route files including tool.py — **LLM/prompt issue**

Scout found only 8 route files out of 24 in `api/routes/`. The `tool.py` file — which contains the key polymorphic `ToolDefinition` discriminated union — was not discovered. Other missed files include `workflow.py`, `organization.py`, `integration.py`, `credentials.py`, `reports.py`, and 11 more.

**Category:** LLM/prompt. The Scout's exploration strategy failed to enumerate all files in the `api/routes/` directory. A simple `glob("api/routes/*.py")` would have found them all. The prescan also returned 0 tentative routes (prescan framework detection failed).

**Impact:** Critical — the entire polymorphism test case (ToolDefinition) was never exercised because the file was never found.

#### ISSUE D2: Pydantic constraint names instead of OpenAPI — **LLM/prompt issue**

`ChunkSearchRequestSchema.limit` has `ge: 1, le: 50` instead of `minimum: 1, maximum: 50`. Same for `min_similarity` with `ge: 0.0, le: 1.0`. This causes a validation error.

**Category:** LLM/prompt. The schema extractor LLM output Pydantic field constraint names (`ge`/`le`) instead of OpenAPI JSON Schema constraint names (`minimum`/`maximum`). The LLM is reading Pydantic source code and faithfully copying Pydantic's naming rather than translating to OpenAPI.

**Impact:** Medium — spec is structurally invalid at these schemas. Any consumer will reject these constraints.

**Possible fix (infra):** Post-processing could deterministically rename `ge`→`minimum`, `le`→`maximum`, `gt`→`exclusiveMinimum`, `lt`→`exclusiveMaximum` in assembled schemas. This is a known Pydantic→OpenAPI translation.

#### ISSUE D3: Duplicate schema extractions — same file extracted up to 7 times — **infra/bug**

`user.py` was extracted 7 times, `looptalk.py` 4 times, `knowledge_base.py` 3 times. Total: 24 schema extraction calls for 10 unique files. Most duplicates hit cache (0ms) but the first call for each is wasted.

**Category:** Infra/bug. The schema loop is re-queuing files that have already been extracted. The deduplication logic in the resolution loop isn't preventing the same file from being scheduled multiple times when multiple ref_hints resolve to the same file.

**Impact:** Low (cache mitigates runtime cost) but wasteful and indicates a logic gap in the schema loop's visited-file tracking.

#### ISSUE D4: `List[TestSessionResponse]` unresolved — **infra/parsing**

The ref_hint `List[TestSessionResponse]` was emitted as-is by the LLM instead of just `TestSessionResponse`. The ref resolver correctly logs "Could not resolve List[TestSessionResponse]" but doesn't strip the `List[...]` wrapper to resolve the inner type.

**Category:** Infra/parsing. The ref resolver should strip generic type wrappers (`List[X]`, `Optional[X]`, `Dict[K, V]`) to extract the inner type name and resolve that instead.

**Impact:** Medium — any response typed as `List[SomeModel]` will fail resolution. This is a common pattern in FastAPI.

#### ISSUE D5: `str, Any` as schema name — **infra/parsing**

A ref_hint of `Dict` resolved to a file but produced no usable schema. The unresolved schema `str, Any` suggests the LLM output `Dict[str, Any]` and infrastructure parsed it into a schema name verbatim.

**Category:** Infra/parsing. `Dict`, `str`, `Any` are Python builtins, not user-defined schemas. The ref resolver should skip these (mark as unresolvable) rather than attempting resolution.

**Impact:** Low — cosmetic but pollutes the schema list.

#### ISSUE D6: Pydantic discriminated unions not detected by ctags inheritance — **infra/gap**

The `ToolDefinition = Annotated[Union[HttpApiToolDefinition, EndCallToolDefinition, TransferCallToolDefinition], Field(discriminator="type")]` pattern uses type aliases and `Union`, not class inheritance. Ctags sees `HttpApiToolDefinition(BaseModel)` — it inherits from `BaseModel`, not from a domain-specific base class. The ctags inheritance feature only detects class hierarchy polymorphism (parent→child), not Pydantic discriminated unions declared via `Annotated[Union[...]]`.

**Category:** Infra/deterministic gap. The ctags inheritance map correctly reports BaseModel as having hundreds of children, but there's no way to know which subset forms a discriminated union without reading the `Union[...]` declaration. This is a fundamentally different polymorphism pattern than class inheritance hierarchies.

**Impact:** High for Python/Pydantic codebases. Discriminated unions via `Annotated[Union[...], Field(discriminator=...)]` are the standard Pydantic pattern for polymorphism. The ctags inheritance feature doesn't help here at all.

---

## 15. letta (Python/FastAPI — large-scale discriminated unions + class inheritance)

**Target polymorphism:**
- `ModelSettingsUnion` — 13 provider subtypes (`OpenAIModelSettings`, `AnthropicModelSettings`, etc.) discriminated by `provider_type`
- `ManagerConfigUnion` — 5 manager subtypes via class inheritance from `ManagerConfig`, discriminated by `manager_type`
- `ToolRuleUnion`, `ResponseFormatUnion`, `ImageSourceUnion`, `LettaMessageUnion` — additional unions

**Pipeline result:** 270 endpoints, 438 schemas, 1 validation error, 32 unresolved schemas

| Metric | Value | Notes |
|--------|-------|-------|
| Route files found | 37 | Scout found extensive route tree |
| Endpoints extracted | 270 | Large API surface |
| Schemas | 438 (414 resolved, 24 unresolved) | 3 resolution rounds |
| Validation errors | 1 | Same ge/le Pydantic constraint issue |
| Unresolved schemas | 32 | Mix of builtins, Union types, external types |
| Security schemes | BearerAuth | Correct |
| Servers | localhost:8083 | Correct |
| Total time | 2340s (~39 min) | |
| LLM calls | 236 | 77 route extraction + 159 schema extraction |

### Issues found

#### ISSUE L1: `ModelSettingsUnion` correctly assembled with oneOf + discriminator — **SUCCESS**

The ctags inheritance feature correctly detected that `OpenAIModelSettings`, `AnthropicModelSettings`, `GoogleAIModelSettings`, etc. all inherit from a common base. The assembler produced:

```yaml
ModelSettingsUnion:
  oneOf:
    - $ref: '#/components/schemas/OpenAIModelSettings'
    - $ref: '#/components/schemas/AnthropicModelSettings'
    # ... 11 more
  discriminator:
    propertyName: provider_type
```

All 13 subtypes present with correct discriminator. This is the ctags inheritance feature working as designed.

#### ISSUE L2: `ManagerConfigUnion` has oneOf but MISSING discriminator — **infra/bug**

`ManagerConfigUnion` correctly has 5 subtypes via `oneOf` but lacks `discriminator: { propertyName: manager_type }`. The source code clearly has `manager_type: Literal[ManagerType.xxx]` on each subtype. `ModelSettingsUnion` got a discriminator but `ManagerConfigUnion` did not.

**Category:** Infra/bug. The assembler's discriminator detection logic is inconsistent — it found `provider_type` on ModelSettings subtypes but missed `manager_type` on Manager subtypes. Likely the discriminator detection relies on enum property heuristics that don't match all patterns.

**Impact:** Medium — without a discriminator, API consumers can't programmatically determine which subtype is present. The oneOf is still correct, just incomplete.

#### ISSUE L3: Union type names emitted as schema names — **infra/parsing**

32 unresolved schemas include raw Python type expressions used as schema names:
- `Union[StdioServerConfig, SSEServerConfig, StreamableHTTPServerConfig]`
- `Union[UpdateStdioMCPServer, UpdateSSEMCPServer, UpdateStreamableHTTPMCPServer]`
- `dict[str, Union[SSEServerConfig, StdioServerConfig, StreamableHTTPServerConfig]]`
- `List[str]`, `Dict`, `str`, `int`, `float`

**Category:** Infra/parsing. The LLM emits Python type annotations as ref_hints, and infrastructure passes them through verbatim as schema names. Two sub-issues:
1. **Builtin types** (`str`, `int`, `float`, `Dict`, `List[str]`) should be mapped to JSON Schema primitives, not treated as schema refs.
2. **Union types** should be decomposed: `Union[A, B, C]` → resolve each of A, B, C individually, then assemble as `oneOf`.

**Impact:** High — 32 unresolved schemas is significant. Many of these are resolvable if the individual types within the Union were extracted.

#### ISSUE L4: Duplicate schema extractions — same file up to 12 times — **infra/bug**

Same issue as dograh but worse at scale: `agents.py` extracted 12x, `enums.py` 9x, `letta_request.py` 6x. Total: 159 schema calls for 69 unique files. Cache mitigates runtime (0ms on hits) but first-time runs waste significant LLM time.

**Category:** Infra/bug. Same root cause as D3 — schema loop re-queues already-processed files. At letta's scale (150 initial pending refs), this amplifies significantly.

**Impact:** Medium — adds ~30% overhead to schema resolution phase even with dedup via cache.

#### ISSUE L5: `ToolRuleUnion`, `ResponseFormatUnion`, `LettaMessageUnion` etc. unresolved — **infra/parsing + LLM**

Several Pydantic discriminated unions that are key API types ended up as unresolved schemas:
- `LettaMessageUnion` — used in message endpoints
- `LettaMessageUpdateUnion` — used in message update endpoints
- `LettaMessageContentUnion` — message content types
- `ResponseFormatUnion` — response format configuration
- `SandboxConfigUnion` — sandbox config types
- `MessageCreateUnion` — message creation types
- `CreateMCPServerUnion`, `UpdateMCPServerUnion` — MCP server types

**Category:** Mixed. The LLM correctly identified these as ref_hints, but they're declared as `Annotated[Union[...], Field(discriminator=...)]` type aliases — same pattern as dograh's `ToolDefinition`. The ref resolver can't resolve a Union type alias name to a single file because it's not a class.

**Impact:** High — these are core API types. Without them, the spec has holes in request/response schemas for many endpoints.

#### ISSUE L6: External/framework types unresolved — **expected/acceptable**

`UploadFile`, `FileResponse`, `ChatCompletion`, `RequestBody`, `OpenAPISchema`, `SwaggerUI`, `ReDoc`, `OpenAITool` — these are framework types from FastAPI, OpenAI SDK, etc. They can't be resolved from the source code.

**Category:** Expected behavior. The system correctly marks these as unresolved with `x-unresolved: true`. A pentester can see these are placeholder schemas.

**Impact:** Low — acceptable gaps.

#### ISSUE L7: Pydantic `ge` constraint in output — **LLM/prompt issue**

Same as dograh D2. At least one schema has `ge:` instead of `minimum:`.

**Category:** LLM/prompt (same root cause as D2).

#### ISSUE L8: `ManagerConfigSchemaUnion` vs `ManagerConfigUnion` — schema name variants — **LLM issue**

The output has both `ManagerConfigSchemaUnion` (from schema extractor) and `ManagerConfigUnion` (from assembler oneOf synthesis). These are likely the same type under different names. The LLM extracted it with a "Schema" suffix that doesn't match the source code name.

**Category:** LLM/prompt. The schema extractor invented a name variant. Infrastructure doesn't normalize schema names, so duplicates appear.

**Impact:** Low — both exist and both have the right subtypes.

---

# Polymorphism Test Summary

## What works

| Feature | Status | Evidence |
|---------|--------|----------|
| Ctags inheritance detection | **Working** | ModelSettingsUnion (13 subtypes), ManagerConfigUnion (5 subtypes) correctly assembled as oneOf |
| Discriminator synthesis | **Partial** | ModelSettingsUnion got discriminator; ManagerConfigUnion did not |
| Multi-round schema resolution | **Working** | Letta resolved 414 schemas across 3 rounds |
| Large codebase handling | **Working** | 270 endpoints, 438 schemas from letta (37 route files) |

## What doesn't work

| Issue | Category | Severity | Repos Affected |
|-------|----------|----------|---------------|
| Scout misses route files | LLM/prompt | Critical | dograh (8/24 found) |
| Pydantic discriminated unions (`Annotated[Union[...]]`) not detected | Infra/gap | High | dograh, letta |
| Union/List/Dict type names as schema names | Infra/parsing | High | dograh, letta |
| Builtin types (`str`, `int`, `Dict`) treated as schema refs | Infra/parsing | Medium | dograh, letta |
| Duplicate schema extractions | Infra/bug | Medium | dograh (2.4x), letta (2.3x) |
| Pydantic `ge`/`le` instead of `minimum`/`maximum` | LLM/prompt | Medium | dograh, letta |
| Inconsistent discriminator detection | Infra/bug | Medium | letta |
| Schema name variants (e.g. added "Schema" suffix) | LLM/prompt | Low | letta |

## Key architectural gap: Pydantic discriminated unions

The ctags inheritance feature detects class hierarchy polymorphism (`class Child(Parent)`) but **not** Pydantic discriminated unions declared as:

```python
MyUnion = Annotated[Union[TypeA, TypeB, TypeC], Field(discriminator="kind")]
```

This is the dominant polymorphism pattern in modern Python/FastAPI codebases. The subtypes inherit from `BaseModel` (not from each other), so ctags sees them as siblings, not as part of a union. The `Union[...]` type alias is a runtime construct invisible to ctags.

**Possible approaches (not implemented):**
1. **LLM-side:** Teach the schema extractor to recognize `Union[...]` with `discriminator` and emit it as a structured oneOf in the schema descriptor.
2. **Infra-side:** Parse `Union[...]` ref_hints by decomposing them into individual types, resolving each, and synthesizing oneOf in the assembler.
3. **Hybrid:** The ref resolver could grep for `Union[` patterns in resolved model files and auto-discover union declarations.

---

# Full Pipeline Results — All 20 Test Repos

Tested across 16 languages/frameworks. Infra fixes applied iteratively; results below reflect the final state.

## Results Summary

| # | Repo | Lang/Framework | Endpoints | Schemas | Val Errors | Unresolved | Status |
|---|------|---------------|-----------|---------|------------|------------|--------|
| 1 | rest-api-node | JS/Express | 10 | 2 | 0 | 0 | Clean |
| 2 | levo-schema-service | Python/FastAPI | 4 | 0 | 0 | 0 | Clean (no ref_hints) |
| 3 | passwordless-auth-rust | Rust/Axum | 29 | 15 | 1 | 1 | LLM issues |
| 4 | spring-boot-blog | Java/Spring | 54 | 29 | 0 | 0 | Clean |
| 5 | aspnetcore-realworld | C#/ASP.NET | 19 | 19 | 0 | 1 | LLM issue |
| 6 | laravel-realworld | PHP/Laravel | 22 | 0 | 0 | 0 | Clean (no ref_hints) |
| 7 | energy-monitoring-app | TS/AWS Lambda | 7 | 5 | 0 | 1 | LLM issue |
| 8 | dograh | Python/FastAPI | 100 | 98 | 1 | 2 | LLM issues |
| 9 | dotnet-clean-architecture | C#/ASP.NET | 8 | 11 | 0 | 0 | Clean |
| 10 | go-gin-ecommerce | Go/Gin | 23 | 27 | 0 | 19 | LLM issues |
| 11 | nestjs-pg-crud | TS/NestJS | 26 | 15 | 0 | 10 | LLM issues |
| 12 | flask-restplus-example | Python/Flask-RESTPlus | 16 | 14 | 0 | 0 | Clean |
| 13 | kotlin-ktor-realworld | Kotlin/Ktor | 38 | 8 | 0 | 1 | LLM issue |
| 14 | rails-rest-api | Ruby/Rails | 14 | 0 | 0 | 0 | Clean (no ref_hints) |
| 15 | swift-vapor-conduit | Swift/Vapor | 10 | 6 | 0 | 6 | LLM issues |
| 16 | elixir-phoenix-api | Elixir/Phoenix | 20 | 3 | 0 | 0 | Clean |
| 17 | haskell-servant | Haskell/Servant | 10 | 4 | 1 | 4 | LLM issues |
| 18 | clojure-compojure | Clojure/Compojure-api | 14 | 7 | 0 | 7 | LLM issues |
| 19 | ocaml-dream | OCaml/Dream | 11 | 7 | 0 | 3 | LLM issues |
| 20 | dart-frog | Dart/Dart Frog | 16 | 10 | 0 | 10 | LLM issues |
| | **TOTALS** | | **451** | **304** | **3** | **65** | |

**Clean repos (8/20):** rest-api-node, levo-schema-service, spring-boot-blog, laravel-realworld, dotnet-clean-architecture, flask-restplus-example, rails-rest-api, elixir-phoenix-api

---

## LLM Limitation Categories

All remaining issues are LLM/prompt problems that cannot be fixed with infra changes.

### L1: Route extractor invents schema names that don't exist in code

**Severity:** High — causes unresolvable refs
**Affected repos:** swift-vapor-conduit (6), nestjs-pg-crud (10), dart-frog (10), haskell-servant (4), clojure-compojure (7)

The LLM creates ref_hint names like `RegisterBody`, `LoginBody`, `UserResponse`, `ProductCreateResponse` that don't correspond to any class/struct/type in the source code. The actual code either:
- Uses **inline anonymous types** (NestJS: `{ email: string; password: string }`)
- Has **no type annotations** (Dart, Clojure)
- Uses **different naming** than what the LLM invented (Swift: `User.RegisterForm` vs LLM's `UserCreateRequest`)

**Root cause:** The route extractor prompt instructs the LLM to provide ref_hints for request/response types. When the code has no explicit type, the LLM invents a plausible name rather than using `resolution: "unresolvable"`.

**Potential fix:** Strengthen the prompt to use `resolution: "unresolvable"` when no explicit type annotation exists in the code. Or add a Pydantic model constraint that validates ref_hint names are actually found in imports.

### L2: Route extractor emits factory function names as schema refs

**Severity:** High — 19 unresolved schemas in go-gin-ecommerce alone
**Affected repos:** go-gin-ecommerce (19)

Go DTOs are implemented as **factory functions** (`func CreateTagListMapDto(tags []models.Tag) map[string]interface{}`) that return `map[string]interface{}`, not as struct types. The LLM emits the function name as a ref_hint (e.g., `TagListMapDto`) even though it's not a type.

**Root cause:** The LLM doesn't distinguish between type definitions and factory functions when generating ref_hints. In Go, DTOs are often constructed via functions rather than defined as structs.

**Potential fix:** Prompt the LLM to only emit ref_hints for actual type definitions (struct, class, interface), never for function names.

### L3: No ref_hints from route-only files

**Severity:** Medium — results in 0 schemas extracted
**Affected repos:** laravel-realworld (0 schemas), rails-rest-api (0 schemas), levo-schema-service (0 schemas)

When route files only contain route registrations with controller references (Laravel's `Route::resource('articles', ArticleController::class)`) or Rails-style `resources :posts`, the LLM has no type information to extract. The actual request/response types are in controller files that aren't part of the route extraction scope.

**Root cause:** Architectural limitation — the route extractor only reads one file per invocation. Laravel/Rails route files are thin dispatchers with no type information. The types live in controllers, form requests, and serializers which are separate files.

**Potential fix:** This is a fundamental architecture question. Options: (a) allow the route extractor to read controller files referenced in routes, (b) add a separate "controller extractor" phase, (c) accept this as a known gap for dispatch-style frameworks.

### L4: Missing request bodies on POST/PUT/PATCH endpoints

**Severity:** Medium — spec is structurally valid but incomplete for pentesting
**Affected repos:** laravel-realworld (10), rails-rest-api (8), kotlin-ktor-realworld (7), spring-boot-blog (4), dograh (23), dotnet-clean-architecture (3), dart-frog (3), aspnetcore-realworld (2)

The LLM doesn't generate `request_body` for endpoints where the body shape isn't explicit in the route file. Some are legitimate (state-toggle PUT endpoints like `completeTodo`), but many are real POST/PUT endpoints where the LLM should infer a body exists.

**Root cause:** Mixed. Some endpoints genuinely have no body (toggles, actions). Others have bodies that are only visible in the controller implementation, not in the route definition. The LLM errs on the side of omission rather than inventing a body.

### L5: Dotted-name schema collision

**Severity:** Low — affects 1 schema in 1 repo
**Affected repos:** aspnetcore-realworld (Edit.Command)

When two files both define a nested class with the same leaf name (`Command` in both `Create.cs` and `Edit.cs`), the dotted-name aliasing can fail. `Create.Command` resolves correctly, but `Edit.Command` resolves to a different file (Users/Edit.cs instead of Articles/Edit.cs) because the import disambiguation points to the wrong file.

**Root cause:** The LLM's import_source for `Edit.Command` points to the Users namespace, not the Articles namespace. The ref resolver correctly follows this hint to the wrong file.

### L6: Ctags not supported for some languages

**Severity:** Low — grep fallback handles most cases
**Affected repos:** swift-vapor-conduit (0 ctags types), dart-frog (0 ctags types)

Universal-ctags has no parser for Swift or Dart. The ctags index returns 0 type definitions. The grep fallback can find type definitions but only if the LLM emitted names matching actual type names in the code (see L1 — it didn't).

**Root cause:** Ctags language support gap. Not fixable in our infra — would need ctags upstream to add parsers.

### L7: OCaml module-scoped type names

**Severity:** Low — 3 unresolved in 1 repo
**Affected repos:** ocaml-dream (User.t, Sensor.t, Reading.t)

The LLM emits OCaml-style type names like `User.t` and `Sensor.t`. The infra now resolves these to the correct files (user.ml, sensor.ml), but the schema extractor produces schemas under different names than `t`. The dotted-name aliasing expects a schema named `t` in the extraction output, but the LLM names them differently.

**Root cause:** The schema extractor doesn't know that OCaml's `t` is the "main type" of a module. It extracts schemas under whatever names make sense to it, which don't match the dotted-name convention.

### L8: External package types as refs

**Severity:** Low — expected and acceptable
**Affected repos:** go-gin-ecommerce (gin.H), nestjs-pg-crud (Express.Multer.File), passwordless-auth-rust (Unknown)

Types from external packages (`gin.H`, `Express.Multer.File`) will never resolve from source code. The system correctly marks them as unresolved with `x-unresolved: true`.

**Not a bug.** A pentester can see these are placeholder schemas from external dependencies.

### L9: Validation error from wildcard/catch-all routes

**Severity:** Low — 1 error each in passwordless-auth-rust, haskell-servant
**Affected repos:** passwordless-auth-rust (`/admin/*`), haskell-servant (`/assets/*`)

The LLM extracts catch-all routes with wildcard path segments (`*`). OpenAPI 3.0 doesn't support wildcard path parameters. The assembler passes them through, causing validation errors.

**Root cause:** The LLM should either skip catch-all routes or rewrite `*` to a named path parameter like `{path}`.

---

## Infra Fixes Applied During This Test Round

| Fix | What | Repos Fixed |
|-----|------|-------------|
| Type hint decomposition | `List[X]`, `Union[A,B]`, `Optional[X]`, `Dict[K,V]` unwrapped before resolution | dograh, letta |
| Builtin type skipping | `str`, `int`, `Dict`, `Any` etc. no longer queued for resolution | dograh, letta |
| Duplicate extraction dedup | Same file not extracted twice within a round | dograh (2.4x→1x), letta (2.3x→1x) |
| Constraint keyword normalization | `ge`→`minimum`, `le`→`maximum`, `min_length`→`minLength`, etc. | dograh, letta |
| Discriminator strategy 2 | Detect discriminator from children's constant property values | letta (ManagerConfigUnion) |
| Primitive type inlining | `$ref` to `String`/`int`/`bool` → inline JSON Schema type | passwordless-auth-rust, dotnet-clean-architecture |
| Comma-separated builtins | `str, Any` → `{type: "object"}` | dograh |
| Primitive refs in paths | `inline_primitive_refs` runs on full spec, not just schemas | dotnet-clean-architecture |
| `module` ctags kind | Elixir/Ruby `defmodule` types now indexed | elixir-phoenix-api |
| Leaked RefHint dicts | Raw RefHint objects in parameter schemas → `$ref` | clojure-compojure |
| Dotted-name filename fallback | `User.t` → match `t` in file named `user.*` | ocaml-dream |
| Space-suffix collection types | `Reading.t list` → decompose to `Reading.t` | ocaml-dream |
| Union ref_hints in assembler | `Union[A, B, C]` → `oneOf` with `$ref` per type | dograh, letta |

---

## Language Coverage Matrix

| Language | Framework | Scout | Routes | Schemas | Auth | Overall |
|----------|-----------|-------|--------|---------|------|---------|
| JavaScript | Express | Good | Good | Good | Good | **Strong** |
| TypeScript | NestJS | Good | Good | Partial (inline types miss) | Good | **Moderate** |
| TypeScript | AWS Lambda | Good | Good | Good | Good | **Moderate** (1 file failed) |
| Python | FastAPI | Good | Good | Good | Good | **Strong** |
| Python | Flask-RESTPlus | Good | Good | Good | Good | **Strong** |
| Java | Spring Boot | Good | Good | Good | Good | **Strong** |
| C# | ASP.NET | Good | Good | Good | Good | **Strong** |
| Rust | Axum | Good | Good | Good | N/A | **Moderate** (over-discovery) |
| PHP | Laravel | Good | Partial | None (route-only) | None | **Weak** |
| Go | net/http | Mixed | Good | Good | N/A | **Moderate** |
| Go | Gin | Good | Good | Partial (factory funcs) | Good | **Moderate** |
| Ruby | Rails | Good | Good | None (route-only) | None | **Weak** |
| Kotlin | Ktor | Good | Good | Good | Good | **Moderate** |
| Swift | Vapor | Good | Good | None (invented names) | None | **Weak** |
| Elixir | Phoenix | Good | Good | Good | N/A | **Strong** |
| Haskell | Servant | Good | Good | None (resolution fail) | N/A | **Weak** |
| Clojure | Compojure-api | Good | Good | None (no ctags) | N/A | **Weak** |
| OCaml | Dream | Good | Good | Partial (name mismatch) | Good | **Moderate** |
| Dart | Dart Frog | Good | Good | None (no ctags + invented names) | N/A | **Weak** |

**Strong (5):** Express, FastAPI, Flask-RESTPlus, Spring Boot, ASP.NET
**Moderate (7):** NestJS, AWS Lambda, Axum, Go net/http, Gin, Ktor, OCaml
**Weak (5):** Laravel, Rails, Swift/Vapor, Haskell/Servant, Clojure, Dart
