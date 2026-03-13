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
