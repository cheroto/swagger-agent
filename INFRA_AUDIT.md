# Infrastructure Audit — Verified Against Pipeline Output

## Executive Summary

**Latest scores:** EP-F1=0.776, SC-F1=0.430, SEC-F1=0.594, AUTH=0.72

Three systemic problems account for ~80% of score losses:

1. **Route file discovery misses files** — Scout doesn't find `user.route.js` (node-express), profile routes (dart-frog), or controller files referenced from registry files (swift-vapor, go-gin). This causes EP-F1=0 in 2 repos and partial losses in 4 more.
2. **Schema extraction produces empty properties** — Schemas are found by name but properties come back as `[]`. Verified in kotlin-ktor (16 schemas, all empty), go-gin (17/45 schemas empty), clojure (4/6 empty). This is the #1 SC-F1 killer.
3. **All ref_hints are `resolution: "unresolvable"`** in repos without explicit cross-file imports — kotlin-ktor, node-express, dart-frog. The Schema Extractor is never called; instead the assembler creates empty placeholder schemas. These are NOT marked `x-unresolved` so they pollute SC-F1 scoring.

---

## Part 1: Infrastructure Fixup Catalog

### Category 1: Path & Parameter Fixups (~120 lines)

**File:** `infra/assembler_pkg/path_utils.py` (208 lines)

| Fixup | Lines | What it does | Root cause |
|-------|-------|-------------|-----------|
| Framework syntax conversion | `78-109` | Converts `:param`, `<param>`, `<int:param>` → `{param}` | LLM doesn't convert to OpenAPI syntax |
| Route constraint stripping | `44-75` | `{param:constraint}` → `{param}`, `{param?}` → `{param}` | LLM leaves framework constraints |
| Param reconciliation | `112-207` | Renames mismatched params, removes orphans, adds missing, forces `required=True` | Param names don't match URL template |

### Category 2: RefHint Parsing (~170 lines)

**File:** `infra/assembler_pkg/assemble.py` (lines 36-206)

| Fixup | Lines | What it does |
|-------|-------|-------------|
| Strip `#/components/schemas/` prefix | `60-64` | LLM puts full `$ref` path in `ref_hint` |
| Regex wrapper parsing | `84-127` | Unwraps `List[T]`, `Optional[T]`, `Vec<T>`, `T[]` |
| Collection/Map wrapper detection | `69-82` | 35+ hardcoded wrapper names (**agnosticism violation**) |
| Union parsing | `130-158` | `Union[A, B, C]` with bracket depth tracking |
| Security scheme derivation | `227-236` | Heuristic: infers scheme from name substring |

### Category 3: Schema Output Fixups (~480 lines)

**File:** `infra/assembler_pkg/schema_fixups.py` (480 lines)

| Fixup | Lines | Frequency with json_schema mode |
|-------|-------|----|
| Leaked RefHint in schema positions | `76-96` | Rare |
| Primitive `$ref` inlining | `99-119` | **Frequent** |
| Constraint keyword renaming | `126-225` | Frequent when constraints exist |
| String → dict coercion | `139-167` | Rare |
| `$ref` + siblings wrapping | `228-253` | Structural — always needed |
| Circular ref breaking | `275-374` | Structural — needed |
| Missing array `items` | `377-393` | Moderate |
| Case normalization | `396-452` | Moderate |
| Duplicate operationId | `455-479` | Moderate |

### Category 4: Auto-Injection & Bypasses

- **401/403 auto-inject** (`assemble.py:372-379`) — masks LLM failure to extract error responses
- **`has_request_bodies` hardcoded True** (`validator.py:333-336`) — completeness check bypassed
- **Case-insensitive schema aliasing** (`loop.py:402-412`) — two-stage case fixing

### Category 5: Agnosticism Violations

| Location | Violation | Verdict |
|----------|-----------|---------|
| `schema_fixups.py:23-55` | 50+ framework-specific primitive types | Necessary |
| `assemble.py:69-82` | 35 collection wrapper names | Avoidable if LLM uses `is_array` |
| `schema_fixups.py:126-136` | 20+ constraint keyword renames | Avoidable with proper `constraints` field |
| `prescan.py:66-83` | Language → extension dispatch table | Acceptable (data, not logic) |

---

## Part 2: Quality-Killing Issues (Verified)

### CRITICAL: SchemaProperty has no `constraints` field

**Location:** `models.py:145-153`

Current fields: `name`, `type`, `format`, `ref`, `is_array`, `nullable`, `enum_values`. No way to express minLength, maxLength, pattern, minimum, maximum.

Live-tested: with field added, LLM outputs perfect JSON Schema constraints. Without it, zero constraints in output.

### CRITICAL: Phase 1 max_tokens=4096 hardcoded

**Location:** `agents/route_extractor/harness.py:108`

Phase 2 uses `config.llm_max_tokens`. Only Phase 1 is hardcoded. On files with 15+ endpoints, `import_lines` and `endpoints` get truncated → cascading failures.

### CRITICAL: Phase 2 prefix `endswith` check produces doubled prefixes

**Location:** `agents/route_extractor/prompt.py:62-64`

```python
if file_prefix and not effective_prefix.endswith(file_prefix):
    effective_prefix = effective_prefix + "/" + file_prefix
```

When `base_path="/api"` and `analysis.base_prefix="/api/v1/users"` → doubled to `/api/api/v1/users`.

---

## Part 3: Per-Repo Gap Analysis — Verified Against Pipeline Output

### EP-F1 Failures

#### flask-restplus-example (EP-F1=0.000)

**Pipeline output:** 16 endpoints with bare paths: `/users/`, `/teams/`, `/auth/oauth2_clients/`
**Golden expects:** Same endpoints with `/api/v1/` prefix

**Verified from output JSON:**
- `manifest.route_files`: `['app/modules/auth/resources.py', 'app/modules/users/resources.py', 'app/modules/teams/resources.py']`
- `manifest.base_path`: `""` (empty!)
- Descriptors show paths like `GET /users/`, `POST /teams/`

**Root cause:** The `/api/v1` prefix lives in `app/__init__.py` where blueprints are registered with `url_prefix`. Scout found route files (the `resources.py` files) but didn't find the registry file or extract the prefix. `base_path` is empty because the Scout never saw the prefix.

**Missing:** Scout/prescan should detect `__init__.py` as a registry file and extract `url_prefix='/api/v1'` into `base_path` or `mount_map`.

#### swift-vapor-conduit (EP-F1=0.000, SC-F1=0.000, everything=0)

**Pipeline output:** `paths: {}` — zero endpoints extracted

**Verified from output JSON:**
- `manifest.route_files`: `['Sources/App/Configuration/routes.swift']`
- `manifest.base_path`: `"api"`
- Descriptors: 1 descriptor for `routes.swift` with **0 endpoints**

**Root cause:** `routes.swift` contains only:
```swift
let api = router.grouped("api")
try api.register(collection: UserController())
try api.register(collection: ArticleController())
```
No actual HTTP methods/paths. The real routes are in `UserController.swift` and `ArticleController.swift` which implement Vapor's `RouteCollection` protocol. These files are **not in route_files** and are never read.

**Missing:** When Route Extractor gets 0 endpoints from a file that has collection/controller registrations, infrastructure should recognize those controllers as route files and extract them.

#### go-gin-ecommerce (EP-F1=0.682)

**Pipeline output:** 21 endpoints with wrong sub-paths

**Verified from output JSON:**
- `manifest.route_files`: `['controllers/users.go', 'controllers/tags.go', ... 'controllers/addresses.go']` (8 files)
- `manifest.base_path`: `"/api"`
- `main.go` NOT in route_files

**Specific path errors (verified from descriptor output):**
| Controller file | Pipeline extracted | Should be | Why wrong |
|---|---|---|---|
| `controllers/products.go` | `GET /api/`, `GET /api/{slug}` | `GET /api/products/`, `GET /api/products/{slug}` | Missing `/products` group prefix |
| `controllers/users.go` | `POST /api/`, `POST /api/login` | `POST /api/users/`, `POST /api/users/login` | Missing `/users` group prefix |
| `controllers/addresses.go` | `GET /api/addresses` | `GET /api/users/addresses` | Missing `/users` group prefix |
| `controllers/comments.go` | `GET /api/products/{slug}/comments` | Correct! | comments.go hardcodes `/products/` |

**Root cause:** `main.go:150-159` registers controllers with group prefixes:
```go
controllers.RegisterProductRoutes(apiRouteGroup.Group("/products"))
controllers.RegisterUserRoutes(apiRouteGroup.Group("/users"))
controllers.RegisterAddressesRoutes(apiRouteGroup.Group("/users"))
```
But `main.go` is not in `route_files`. The Route Extractor reads `products.go` which only has `router.GET("/", ...)` — no prefix info. The mount_map mechanism should catch this, but Phase 1 of `products.go` doesn't produce a mount_map (it defines endpoints, not mounts).

**Missing:** `main.go` needs to be included as a registry/entry-point file. Its group registrations need to be parsed and injected as mount prefixes for each controller file.

#### node-express-boilerplate (EP-F1=0.727)

**Pipeline output:** 9 endpoints (all `/v1/auth/*` and `/v1/docs/`). Missing all `/v1/users/*`.

**Verified from output JSON:**
- `manifest.route_files`: `['src/routes/v1/index.js', 'src/routes/v1/auth.route.js', 'src/routes/v1/docs.route.js']`
- **`src/routes/v1/user.route.js` EXISTS but is NOT in route_files**
- `index.js` extracted 0 endpoints (it's a router-mounting file, not an endpoint file)

**Root cause:** `index.js` requires and mounts `user.route.js` at `/users`, but Scout didn't identify `user.route.js` as a route file. The prescan or Scout's glob/grep patterns missed it despite it being named `*.route.js` like the others.

**Why `index.js` produced 0 endpoints:** It only does `router.use(route.path, route.route)` — no actual GET/POST handlers. This is correct behavior. But the mount_map from Phase 1 of index.js should have triggered extraction of `user.route.js`. Either Phase 1 didn't produce a mount_map, or the mount resolution didn't map `userRoute` → `user.route.js`.

#### dart-frog (EP-F1=0.842)

**Pipeline output:** Missing 3 profile endpoints

**Verified from manifest:**
- `manifest.route_files`: Only includes `routes/index.dart`, `routes/auth/*`, `routes/products/*`
- **Missing:** `routes/profile/index.dart`, `routes/profile/[id]/index.dart`, `routes/profile/[id]/favorite.dart`
- These files exist on disk but were not discovered by Scout

**Root cause:** Scout's route file discovery glob missed the `profile/` directory. Dart Frog uses file-based routing where every `.dart` file under `routes/` is a route. The Scout should glob `routes/**/*.dart` but apparently missed the profile subtree.

#### kotlin-ktor-realworld (EP-F1=0.895)

**Pipeline output:** `GET /users/user` and `PUT /users/user` instead of `GET /user` and `PUT /user`

**Verified from descriptor:** Route Extractor produced these paths from `Router.kt`

**Source code shows two SIBLING `route()` blocks:**
```kotlin
fun Routing.users(userController: UserController) {
    route("users") {
        post { ... }           // POST /users
        post("login") { ... }  // POST /users/login
    }
    route("user") {            // This is a SIBLING, not nested!
        authenticate {
            get { ... }        // GET /user (NOT /users/user)
            put { ... }        // PUT /user (NOT /users/user)
        }
    }
}
```

**Root cause:** LLM interpreted `route("user")` as nested under `route("users")` because they're both inside the `users()` function. The function name `users` confused the LLM into thinking everything is under `/users`.

#### ocaml-dream (EP-F1=0.727)

**Pipeline output:** `/api`, `/api/version`, `/api/sensor/upload` etc.
**Golden expects:** `/`, `/version`, `/sensor/upload` (without `/api` prefix for some routes)

**Root cause:** Pipeline adds `base_path="/api"` as prefix to all routes. But some routes in `server.ml` are defined at the root level (`/`, `/version`) while others are under `/api`. The blanket `base_path` application over-prefixes root-level routes.

#### passwordless-auth-rust (EP-F1=0.762)

**Pipeline output:** Has `/magic/verify`, `/magic/request`. Missing `/health`, `/liveness`, `/metrics`, `/readiness`.

**Root cause:** Health/metrics endpoints are defined in a different router or in `main.rs`, not in the `routes.rs` file that Scout found. Scout identified only one route file.

### SC-F1 Failures — THE BIG FINDING

#### kotlin-ktor-realworld (SC-F1=0.000) — ALL SCHEMAS HAVE EMPTY PROPERTIES

**Verified from pipeline output:**
- 16 schemas generated: `RegisterUserRequest`, `UserResponse`, `LoginRequest`, etc.
- **ALL have `properties: []`** — not a single property extracted
- **ALL ref_hints are `resolution: "unresolvable"`** — Route Extractor could not find imports

**Actual source:** Kotlin `data class User(val id: Long?, val email: String, val token: String?, ...)` in `domain/User.kt`. Properties are clearly defined.

**Root cause chain:**
1. `Router.kt` (the only route file) has NO import statements for domain types — Kotlin uses same-package implicit access
2. Route Extractor marks ALL schema refs as `resolution: "unresolvable"` because there are no import lines
3. Schema resolution loop sees all unresolvable → creates placeholder schemas
4. **Placeholders are NOT marked `x-unresolved: true`** — they're plain empty objects
5. Schema Extractor is **never called** for any Kotlin file
6. Result: 16 schemas exist but all have zero properties

**The golden expects:** `User`, `UserDTO`, `Article`, `ArticleDTO`, etc. — the actual Kotlin domain classes. These are completely different names from what the pipeline generated (which invented names like `RegisterUserRequest`, `UserResponse`).

#### go-gin-ecommerce (SC-F1=0.355) — MIXED: Some extracted, many empty

**Verified from pipeline output:**
- 45 total schemas: 17 with properties, 17 empty (not x-unresolved), 11 x-unresolved
- Resolution breakdown: 27 `import`, 0 `class_to_file`, 45 `unresolvable`

**Schemas WITH properties (extracted correctly):**
`RegisterRequestDto`, `LoginRequestDto`, `CreateTag`, `BaseDto`, `ErrorDto`, `CreateOrderRequestDto`, `CreateProduct`, `CreateComment`, `CreateAddress`, `models.Product`, `Tag`, `FileUpload`, `Comment`, `Category`, etc.

**Schemas WITHOUT properties (failed extraction):**
`User`, `Product` (domain models), `gin.H`, `TagListMapDto`, `ProductDetailsDto`, `HomeResponse`, etc.

**Root cause:** Go DTO functions return `map[string]interface{}` (dynamically constructed maps), not typed structs. The functions like `CreateProductDto()` build response maps inline. The Schema Extractor can extract struct fields (`CreateProduct` has 4 fields from struct tags) but can't extract properties from dynamic map construction.

The domain models (`User`, `Product`) exist as structs in `models/` with correct GORM tags but were extracted with empty properties — the Schema Extractor was called but failed to parse them.

#### clojure-compojure (SC-F1=0.000) — Names match, properties empty

**Verified from pipeline output:**
- 6 schemas: `Total`, `MathMinusBody`, `NewSingleToppingPizza`, `EchoAnonymousBody`, `Pizza`, `NewPizza`
- **ALL have `properties: []`**

**Actual source (`domain.clj`):**
```clojure
(s/defschema Pizza {:id Long, :name String, :price Double, :hot Boolean,
                    (s/optional-key :description) String, :toppings #{Topping}})
```

**Root cause:** The Schema Extractor receives the Clojure file but can't parse `defschema` map literals into `SchemaProperty` objects. The LLM identifies the schema names correctly but fails to extract the key-value pairs as properties. This is a language-specific parsing failure — Clojure's `{:key Type}` syntax is fundamentally different from `class { field: Type }`.

**Note:** The scorer gives SC-F1=0.0 even though names match (Pizza, NewPizza, etc.) because the scoring is 30% name + 70% property overlap. With 0 properties, the Dice coefficient is 0.

#### haskell-servant (SC-F1=0.000)

**Verified:** Golden expects `User` and `EntityUser`. Pipeline generated `User` (empty properties) and `ListUser`.

**Root cause:** Template Haskell `persistLowerCase` generates types at compile time. Source shows `share [mkPersist sqlSettings, ...]` — the actual field definitions are inside a quasi-quoter that the LLM can't read as normal Haskell.

#### nestjs-pg-crud (SC-F1=0.384) — Over-extraction + partial failures

**Verified:**
- Golden: 9 schemas. Generated: 27 schemas.
- Core matches are EXCELLENT: `User` has all 13 expected properties. `City` perfect. `CreateCityDto` perfect. `UpdateProfileDto` perfect. `ChangePasswordDto` perfect.
- **Failures:** `UpdateUserDto` has 2/10 expected properties. `UpdateCityDto` has 0/5 properties. `CreateUserDto` missing entirely. `AuditLog` missing.
- **18 extra schemas** not in golden: auth DTOs, upload DTOs, health response, etc.

**Root cause:** Two issues:
1. Schema Extractor partially fails on some DTO files (UpdateUserDto, UpdateCityDto) — likely the file has complex validation decorators that confuse extraction
2. Over-extraction: the pipeline extracts ALL reachable schemas, not just the ones relevant to golden expectations. The golden file may be conservative.

#### node-express-boilerplate (SC-F1=0.000)

**Verified:**
- Golden expects: `AuthTokens`, `Error`, `Token`, `User`, `UserListResponse`
- Generated: `RegisterRequest`, `LoginRequest`, `LoginResponse`, etc. — **all with empty properties**
- ALL ref_hints are `resolution: "unresolvable"` — no imports found in `auth.route.js`

**Root cause:** Same as kotlin-ktor. Express route files use `require('./controllers/auth.controller')` but don't import the model types directly. The Route Extractor can't map response types to file paths. Schema Extractor is never called.

The Mongoose models (`user.model.js`, `token.model.js`) exist with clear schemas but are never discovered because there's no import chain from the route files to the model files.

#### laravel-realworld (SC-F1=0.167)

**Verified:**
- Only 1 of 11 golden schemas partially matched
- Golden expects response envelope types (`ArticleResponse`, `UserResponse`, etc.)
- Generated has different naming (`ArticleList`, `AuthResponse`, etc.)

**Root cause:** Laravel uses Eloquent models + Transformers/Resources for responses. The golden expects the response envelope shapes, but the pipeline extracts what it can find via imports — which are mostly request validation classes, not response classes.

### AUTH Failures

#### rails-rest-api (AUTH=0.14, SEC-F1=0.0)

**Verified from source:**
- `config/routes.rb` has NO auth info — just `resources :posts` and `post "login"`
- `ApplicationController` includes `TokenAuthenticatable` concern
- `concerns/token_authenticatable.rb:5` has `before_action :authenticate_request`
- `authentication_controller.rb:2` and `users_controller.rb:2` have `skip_before_action :authenticate_request`

**Root cause:** Auth is applied in a concern included by `ApplicationController`. ALL controllers inherit it. Only `authentication_controller` and `users_controller` skip it. The Route Extractor reads `routes.rb` (no auth info), not the controllers.

**What the spec SHOULD show:** All endpoints except `POST /register` and `POST /login` need auth. Currently all are marked public.

#### laravel-realworld (AUTH=0.35, SEC-F1=0.0)

**Verified:** `routes/api.php` has route definitions but no visible middleware groups. Auth middleware is applied via `RouteServiceProvider` or Kernel, not inline.

#### dart-frog (AUTH=0.38, SEC-F1=0.0)

**Verified:** `routes/products/_middleware.dart` exists with `bearerAuthentication<User>()`. This file is NOT in route_files. Route Extractor never sees the auth requirement.

#### kotlin-ktor-realworld (AUTH=0.77)

**Verified from source:** `Router.kt` has `authenticate(optional = true)` blocks wrapping public read endpoints (GET /articles, GET /tags, etc.). The Route Extractor marks these as requiring auth, but `optional = true` means auth is optional — they're public endpoints that accept optional auth for personalization.

#### aspnetcore-realworld (SEC-F1=0.0)

**Verified:** Pipeline generated `BearerAuth` with `type: http`. Golden expects `Bearer` with `type: apiKey`. Source code (`Program.cs`) explicitly uses `SecuritySchemeType.ApiKey` in the Swagger config. **The golden is correct — pipeline misidentified the scheme type.**

#### ocaml-dream (SEC-F1=0.5)

**Verified:** Pipeline generated `CookieAuth` with `type: http`. Golden expects `CookieAuth` with `type: apiKey`. The auth uses Dream's cookie/session system. Cookie auth maps to `apiKey` (cookie location) in OpenAPI, not `http`. **Golden is correct.**

---

## Part 4: Root Cause Taxonomy

### A. Route File Discovery Gaps (affects EP-F1)

| Pattern | Repos | What Scout misses |
|---------|-------|-------------------|
| File-based routing: not all route directories found | dart-frog | `routes/profile/` directory missed |
| Named route files in same directory not found | node-express | `user.route.js` exists alongside `auth.route.js` but not discovered |
| Controller files registered from central router | swift-vapor, go-gin | Controllers implement routes but aren't in route_files |
| Registry/entry-point files not parsed for prefix | flask-restplus, go-gin, laravel | `__init__.py`, `main.go`, `RouteServiceProvider.php` hold prefix info |

### B. Ref Resolution Failures (affects SC-F1)

| Pattern | Repos | What breaks |
|---------|-------|-------------|
| Same-package implicit imports (no import lines) | kotlin-ktor, go-gin (partial) | ALL ref_hints → `unresolvable`, Schema Extractor never called |
| Dynamic response construction (`map[string]interface{}`) | go-gin | DTO functions build maps inline, no typed response struct |
| Route file doesn't import model types | node-express | `auth.route.js` requires controller, not models |
| DSL/macro schema definitions | clojure | `defschema` map literals can't be parsed as class fields |
| Compile-time generated types | haskell | Template Haskell quasi-quoters |

### C. Empty Properties Extraction (affects SC-F1)

**This is distinct from ref resolution.** Even when Schema Extractor IS called, some schemas come back with empty properties:

| Repo | Schemas with empty props | Why |
|------|-------------------------|-----|
| go-gin | `User`, `Product`, 15 DTOs | Domain models extracted but properties not parsed from GORM struct tags |
| clojure | ALL 6 schemas | `defschema` map syntax not parseable |
| kotlin-ktor | ALL 16 schemas | Never extracted — assembled from unresolvable refs |
| nestjs | `UpdateUserDto`, `UpdateCityDto` | Partial extraction failure — complex decorator patterns |

### D. Auth Detection Failures (affects AUTH, SEC-F1)

| Pattern | Repos | What's invisible |
|---------|-------|-----------------|
| Base class `before_action` / concern inclusion | rails | `ApplicationController` includes `TokenAuthenticatable` |
| Directory-scoped middleware files | dart-frog | `_middleware.dart` in route directories |
| Route group middleware (not inline) | laravel | Middleware applied in `RouteServiceProvider` |
| Optional auth (`authenticate(optional=true)`) | kotlin-ktor | LLM treats optional auth as required |
| Scheme type misidentification | aspnetcore, ocaml | `apiKey` misidentified as `http` |

---

## Part 5: Prioritized Fix List

### Tier 1: Route File Discovery (EP-F1 impact: +0.10-0.15)

**1. Scout must find ALL route files in file-based routing frameworks**

dart-frog, Next.js-style routing: glob `routes/**/*.dart` should find everything under `routes/`. Currently misses `profile/` subtree. This is likely a Scout glob pattern or prescan issue.

**2. Scout must find sibling route files**

node-express: `user.route.js` is in the same directory as `auth.route.js` and `docs.route.js`. If prescan/Scout finds `auth.route.js`, it should also find `user.route.js` by the same pattern (`*.route.js` in same directory).

**3. Parse mount_map from index/registry files to discover sub-route files**

node-express `index.js` imports `user.route.js` via `require('./user.route')`. If `index.js` is in route_files and Phase 1 produces a mount_map (`{userRoute: '/users'}`), the infrastructure should:
- Resolve `userRoute` → `user.route.js`
- Add it to route_files
- Re-extract with mount prefix `/users`

### Tier 2: Prefix Injection (EP-F1 impact: +0.10-0.15)

**4. Extract prefix from registry/entry-point files**

- flask-restplus: Parse `__init__.py` for `url_prefix='/api/v1'` in Blueprint registration
- go-gin: Parse `main.go` for `apiRouteGroup.Group("/products")` → inject `/products` as mount prefix for `products.go`
- laravel: Parse `RouteServiceProvider.php` for `Route::prefix('api')`

This doesn't require reading registry files AS route files. It means: prescan identifies them, a lightweight pass extracts prefix/mount info, and that info is injected into Route Extractor Phase 2 context.

**5. Fix prefix deduplication `endswith` bug**

`prompt.py:62-64` — prevents doubled prefixes like `/api/api/v1`.

### Tier 3: Schema Quality (SC-F1 impact: +0.10-0.15)

**6. Add `constraints` dict to SchemaProperty**

Live-tested: perfect extraction when field exists. Highest single-value fix for pentest utility.

**7. Fix unresolvable ref_hints producing non-x-unresolved empty schemas**

When ref_hints are `resolution: "unresolvable"` and the Schema Extractor is never called, the assembler creates schemas with empty properties. These MUST be marked `x-unresolved: true` so they're excluded from scoring and clearly identified as placeholders. Currently they appear as "real" schemas, misleading both the scorer and consumers.

**8. Implement `class_to_file` resolution via ctags**

For kotlin-ktor and similar repos: when Route Extractor marks refs as `unresolvable` but ctags finds the class in the project, use ctags to resolve the file path and trigger Schema Extractor.

**9. Fix known_schemas substring match**

`loop.py:336-339`: Use `re.search(rf'\b{re.escape(n)}\b', file_text)` instead of `n in file_text`.

**10. Cap known_schemas size**

Token budget or import-line priority ranking.

### Tier 4: Auth Detection (AUTH impact: +0.15-0.20)

**11. Cross-file auth context injection**

When Phase 1 detects auth-related imports but no per-endpoint markers, read the base class/concern file and inject auth pattern info into Phase 2. Affects rails (AUTH 0.14→~0.85), dart-frog (AUTH 0.38→~0.80).

**12. Distinguish optional from required auth**

kotlin-ktor: `authenticate(optional = true)` should be treated as public (`security: []`). Add guidance to Phase 2 prompt or CodeAnalysis to distinguish optional auth.

**13. Fix security scheme type detection**

aspnetcore produces `type: http` when source explicitly uses `SecuritySchemeType.ApiKey`. ocaml produces `type: http` for cookie-based session auth. Both should be `apiKey`.

### Tier 5: Infra Cleanup

**14. Fix Phase 1 max_tokens=4096 hardcoded** — Use `config.llm_max_tokens`.

**15. Remove 401/403 auto-injection** — Let completeness check report honestly.

**16. Re-enable `has_request_bodies` check**.

### Tier 6: Accept as Limitations

**17. Clojure `defschema` map literals** — LLM can identify names but can't extract properties from `{:key Type}` syntax. Impact: 1 repo.

**18. Haskell Template Haskell** — Compile-time type generation. Impact: 1 repo.

**19. Swift Vapor `RouteCollection` protocol** — Routes defined in controllers, not visible from `routes.swift`. Partially addressable by Tier 1 #3 (discover controllers from registration calls).

**20. Go dynamic map responses** — `map[string]interface{}` response construction can't be extracted as typed schemas. The struct-based domain models CAN be extracted (and are), but the DTO response functions are opaque.

---

## Part 6: Metric Projections

### If Tier 1-2 applied (5 fixes — route discovery + prefix injection):

| Metric | Current | Projected | Basis |
|--------|---------|-----------|-------|
| EP-F1 | 0.776 | ~0.88 | dart-frog +3 endpoints, node-express +5 endpoints, go-gin paths fixed, flask prefix fixed |
| SC-F1 | 0.430 | ~0.45 | Minimal — schema issues are ref resolution, not route discovery |
| AUTH | 0.72 | ~0.75 | More matched endpoints to score auth on |

### If Tier 1-3 applied (10 fixes — + schema quality):

| Metric | Current | Projected | Basis |
|--------|---------|-----------|-------|
| EP-F1 | 0.776 | ~0.88 | Same as above |
| SC-F1 | 0.430 | ~0.55 | class_to_file unlocks kotlin-ktor; x-unresolved marking improves accuracy; known_schemas fix reduces pollution |
| AUTH | 0.72 | ~0.75 | Same |

### If Tier 1-4 applied (13 fixes — + auth detection):

| Metric | Current | Projected | Basis |
|--------|---------|-----------|-------|
| EP-F1 | 0.776 | ~0.88 | Same |
| SC-F1 | 0.430 | ~0.55 | Same |
| SEC-F1 | 0.594 | ~0.72 | rails, dart-frog, aspnetcore, ocaml get correct schemes |
| AUTH | 0.72 | ~0.85 | rails 0.14→0.85, dart-frog 0.38→0.80, kotlin-ktor 0.77→0.95 |

---

## Appendix A: Per-Repo Pipeline Output Summary

| Repo | Route Files Found | Base Path | Endpoints | Schemas | Schemas w/ Props | All Refs Unresolvable? |
|------|-------------------|-----------|-----------|---------|-------------------|----------------------|
| aspnetcore | 5 files | "" | 23 | ~20 | ~15 | No |
| clojure | 1 file | "" | 14 | 6 | **0** | Partial |
| dart-frog | 6 files (missing profile/) | "" | 8/11 | ~8 | ~3 | Partial |
| flask-restplus | 3 files | **""** (should be /api/v1) | 16 | 16 | 11 | No |
| go-gin | 8 files (missing main.go) | "/api" | 21 | 45 | 17 | 45/72 |
| haskell | 1 file | "/api" | 8 | 3 | **0** | Partial |
| kotlin-ktor | 1 file | "" | 19 | 16 | **0** | **YES** |
| laravel | 1 file | "/api" | 31 | 14 | ~5 | Partial |
| nestjs | 5 files | "" | 26 | 27 | ~20 | No |
| node-express | 3 files (missing user.route.js) | "/v1" | 9/14 | 8 | **0** | **YES** |
| ocaml | 1 file | "/api" (over-applied) | 8/11 | ~8 | ~4 | No |
| passwordless-rust | 1 file (missing health routes) | "" | 10 | 9 | 8 | Partial |
| rails | 1 file | "" | 14 | 6 | 4 | No |
| rest-api-node | 1 file | "" | 11 | 2 | 2 | No |
| spring-boot | 4 files | "/api" | 25 | ~20 | ~18 | No |
| swift-vapor | 1 file | "api" | **0** | **0** | **0** | N/A |

## Appendix B: Bloat Reduction Summary

| Module | Lines | Compensation % | Deletable if LLM improves |
|--------|-------|----------------|--------------------------|
| `path_utils.py` | 208 | ~60% | ~60 lines (syntax conversion + constraint stripping) |
| `assemble.py` | 487 | ~35% | ~80 lines (collection wrappers, prefix stripping) |
| `schema_fixups.py` | 480 | ~70% | ~140 lines (primitive refs, constraint renames, string coercion) |
| `loop.py` | 632 | ~10% | ~20 lines (case aliasing) |
| **Total eliminable** | | | **~300 lines** |

Remaining ~500 lines are genuinely necessary: circular ref breaking, OAS 3.0 `allOf` wrapping, case normalization, operationId dedup, param `required=True` enforcement.

---

## Part 7: Deep Trace — Exact Bug Locations

### BUG 1: Express route pattern missing `.route()` method

**File:** `swagger_agent/infra/detectors/routes/javascript.py:7`

```python
"express": [
    ("**/*.{js,ts}", r"(router|app)\.(get|post|put|patch|delete|all)\s*\("),
],
```

**Problem:** The regex matches `router.post(`, `router.get(`, etc. but NOT `router.route(`. Express's `router.route('/path').get().post()` pattern chains methods off `.route()`, not directly on `router`.

**Verified:** `auth.route.js` uses `router.post('/register', ...)` → **matches**. `user.route.js` uses `router.route('/').post(...)` → **does not match**. Both files are in the same directory, same naming convention. The regex is the sole reason `user.route.js` is missed.

**Fix:** Add `|route` to the method group:
```python
("**/*.{js,ts}", r"(router|app)\.(get|post|put|patch|delete|all|route)\s*\("),
```

**Impact:** node-express-boilerplate EP-F1 0.727→~0.95 (recovers all 5 missing `/v1/users/*` endpoints).

### BUG 2: Base path detection stops at first `app.use()` match

**File:** `swagger_agent/infra/detectors/servers/detect.py:140-155`

```python
for rel_path in candidates[:30]:
    full = os.path.join(target_dir, rel_path)
    content = read_file_safe(full, max_bytes=8000)
    for pattern, _source in _BASE_PATH_PATTERNS:
        m = re.search(pattern, content)
        if m:
            base_path = m.group(1)
            notes.append(f"Found base path '{base_path}' in {rel_path}")
            break
    if base_path:
        break  # ← Stops after FIRST match across ALL files
```

**Problem:** The first `_BASE_PATH_PATTERNS` match in the first matching file wins. For node-express-boilerplate, `src/app.js` has:
- Line 50: `app.use('/v1/auth', authLimiter)` — rate-limiter middleware, NOT a route mount
- Line 54: `app.use('/v1', routes)` — actual route mount

The regex `app\.use\s*\(\s*['"](/...)['"` matches `/v1/auth` first → `base_path="/v1/auth"` (WRONG). The correct value is `/v1`.

**Verified:** `re.finditer()` on `app.js` produces matches `["/v1/auth", "/v1"]` in that order. The code takes the first.

**This cascades:** When mount_prefix `"/auth"` is appended to base_path `"/v1/auth"`, Phase 2 gets `effective_prefix="/v1/auth/auth"` → all auth endpoint paths are doubled.

**Fix options:**
1. Take the SHORTEST match (most general prefix)
2. Skip patterns where the second argument to `app.use()` is not a router/routes import
3. Prefer matches from entry point files (`app.js`, `index.js`, `main.js`) and take the one that mounts the main router

### BUG 3: Unresolvable refs produce unmarked empty schemas

**File:** `swagger_agent/infra/schema_loop_pkg/loop.py:309-317`

```python
llm_res = llm_resolution_by_name.get(schema_name, "")
if llm_res == "unresolvable":
    all_schemas[schema_name] = {"type": "object"}     # ← BUG: no x-unresolved
else:
    all_schemas[schema_name] = {
        "type": "object",
        "description": "Schema could not be resolved from source code.",
        "x-unresolved": True,                          # ← CORRECT
    }
```

**Problem:** When the Route Extractor marks a ref as `resolution: "unresolvable"` (line 310) AND ctags fails to resolve it (line 292), the schema is created as a bare `{"type": "object"}` with NO `x-unresolved` marker. But when the LLM expected resolution (`import` or `class_to_file`) and ctags fails, it IS marked `x-unresolved`.

The comment on line 306-307 explains the intent:
> "If the LLM already classified this as 'unresolvable', ctags failing is expected — don't flag as x-unresolved."

**But this is wrong.** The downstream impact is:
1. The assembler copies these bare objects into `components/schemas` (assemble.py:412-413)
2. The scorer sees schemas without `x-unresolved` and counts them as real schemas
3. They have empty properties → property Dice coefficient = 0 → drags down SC-F1
4. They're NOT excluded from scoring → worse than if they didn't exist at all

**Verified for kotlin-ktor:** All 16 ref_hints are `resolution: "unresolvable"`. All 16 become `{"type": "object"}` without `x-unresolved`. Schema Extractor is never called.

**Fix:** Mark ALL unresolved schemas with `x-unresolved: true` regardless of LLM classification:
```python
if llm_res == "unresolvable":
    all_schemas[schema_name] = {
        "type": "object",
        "description": "Type marked unresolvable by extraction agent.",
        "x-unresolved": True,
    }
```

**Impact on scoring:** kotlin-ktor SC-F1 0.000→still 0.000 (no TRUE schemas extracted), but the 16 empty schemas stop dragging down precision. node-express SC-F1 0.000→still 0.000 for same reason. The AVERAGE SC-F1 would improve because precision stops being penalized by empty shells.

### BUG 4: No Dart or Swift route detectors exist

**Missing files:**
- `swagger_agent/infra/detectors/routes/dart.py` — does not exist
- `swagger_agent/infra/detectors/routes/swift.py` — does not exist

**Registry:** `swagger_agent/infra/detectors/routes/_registry.py:11-20` imports: `javascript, python, java, go, ruby, rust, php, csharp`. No dart. No swift.

**Framework detectors DO exist:** `swagger_agent/infra/detectors/framework/` has NO dart.py or swift.py either. But the Scout agent can detect these frameworks independently via code analysis.

**Dart Frog impact:** Prescan returns 0 route files → Scout must discover ALL routes by itself. Scout found 6 of 9 route files (missed `routes/profile/` subtree). If prescan had a Dart pattern like `("**/routes/**/*.dart", r"(Future<Response>|Response)\s+\w+\s*\(")`, it would find all 9.

**Swift Vapor impact:** Prescan returns 0 route files → Scout found `routes.swift` (correct) but not controller files. Even with a Swift detector, the pattern would need to match Vapor's `RouteCollection` protocol implementations in controller files to be useful.

### BUG 5: Go package imports invisible to `_find_importers()`

**File:** `swagger_agent/infra/detectors/prescan.py:42-63`

**Problem:** `_find_importers()` builds search terms from route file paths: `"controllers/users.go"` → terms `["users.go", "controllers/users"]`. It then greps for these terms in quoted strings.

Go imports use module paths: `"github.com/melardev/GoGonicEcommerceApi/controllers"` — this is a PACKAGE import, not a file import. The search term `"controllers/users"` is NOT a substring of this import.

**Verified:** None of the 6 generated search terms (`users.go`, `controllers/users`, `products.go`, `controllers/products`, `tags.go`, `controllers/tags`) appear in `main.go`.

**Impact:** `main.go` is never identified as an importer → its `RegisterProductRoutes(apiRouteGroup.Group("/products"))` calls are invisible → mount prefixes `/products`, `/users`, `/tags` never injected → paths are wrong for products.go, users.go, addresses.go.

**The interesting thing:** `comments.go` has CORRECT paths (`/api/products/{slug}/comments`) because the comments controller hardcodes the full path including `/products/` prefix. Other controllers use relative paths (`/`, `/{slug}`) that need the group prefix.

**Fix options:**
1. Add Go-specific importer detection: search for `".../<package_name>"` import patterns where package_name is the parent directory of route files
2. Search for function calls: `controllers.RegisterProductRoutes` contains the package name `controllers` which matches the parent directory
3. The mount_map approach could work if `main.go` were included in route_files — Phase 1 of `main.go` would produce a mount_map

### BUG 6: Phase 2 prefix combination is wrong

**File:** `swagger_agent/agents/route_extractor/prompt.py:56-64`

```python
effective_prefix = base_path.rstrip("/")
if mount_prefix:
    effective_prefix = effective_prefix + "/" + mount_prefix.strip("/")
if analysis.base_prefix:
    file_prefix = analysis.base_prefix.strip("/")
    if file_prefix and not effective_prefix.endswith(file_prefix):
        effective_prefix = effective_prefix + "/" + file_prefix
```

**Three sub-bugs:**

1. **mount_prefix appended to wrong base_path** (line 59): When `base_path="/v1/auth"` (BUG 2) and `mount_prefix="/auth"`, result is `/v1/auth/auth`. Even with correct `base_path="/v1"`, the combination would be `/v1/auth` — correct. So this bug is a CONSEQUENCE of BUG 2.

2. **`endswith` check for file_prefix** (line 63): When `base_path="/api"` and `analysis.base_prefix="/api/v1/users"`, `file_prefix="api/v1/users"`. `"/api".endswith("api/v1/users")` is False → appended → `/api/api/v1/users`. Should use path-segment containment check.

3. **No deduplication of overlapping prefixes**: When mount_prefix already contains base_path segments, they can double up. Example: mount_prefix="/api/users" with base_path="/api" → `/api/api/users`.

### BUG 7: Schema Extractor produces empty properties for Go structs

**Verified from pipeline output:** go-gin-ecommerce has 17 schemas with 0 properties that are NOT x-unresolved. These were schemas where ctags found the file and Schema Extractor was called, but the LLM returned empty `properties: []`.

Example: `models/user.go` has clear struct fields:
```go
type User struct {
    gorm.Model
    FirstName string `gorm:"varchar(255);not null"`
    LastName  string `gorm:"varchar(255);not null"`
    Email     string `gorm:"column:email;unique_index"`
    ...
}
```

The Schema Extractor received this file but returned `User` with 0 properties. Possible causes:
- `gorm.Model` embedding confuses the LLM (should expand to ID, CreatedAt, UpdatedAt, DeletedAt)
- GORM struct tags (not JSON tags) may confuse the LLM about serialization names
- The `known_schemas` context may have been too large, pushing file content out of context

**This needs a live test to diagnose** — rerun Schema Extractor on this specific file with debug logging to see what the LLM receives and returns.

---

## Part 8: Dependency Map — What Fixes Unblock What

```
BUG 1 (Express .route() regex)
  └→ Fixes: node-express user.route.js discovery
     └→ But ALSO needs BUG 2 fix for correct prefix
        └→ Which ALSO needs BUG 6 fix for correct combination

BUG 2 (base_path first-match)
  └→ Fixes: node-express base_path "/v1" (was "/v1/auth")
     └→ Combined with BUG 6 fix → correct "/v1/auth", "/v1/users" paths

BUG 3 (unmarked unresolvable schemas)
  └→ Fixes: SC-F1 precision for kotlin-ktor, node-express (stops counting empty schemas)
     └→ But does NOT fix extraction (schemas still empty)
        └→ Needs class_to_file resolution (Tier 3 #8) for actual schema content

BUG 4 (missing Dart/Swift detectors)
  └→ Fixes: dart-frog prescan (Scout currently compensates partially)
  └→ Fixes: swift-vapor prescan (Scout can't compensate — controllers not found)

BUG 5 (Go package imports)
  └→ Fixes: go-gin mount prefix discovery (main.go identified as importer)
     └→ But needs mount_map Phase 1 on main.go → needs main.go in route_files

BUG 6 (prefix combination)
  └→ Fixes: prefix doubling for any repo with overlapping base_path and analysis.base_prefix

BUG 7 (empty Go struct extraction)
  └→ Independent — Schema Extractor quality issue, not pipeline bug
```

### Quick Win Stack (3 bugs, ~10 lines changed):

1. **BUG 1** — Add `|route` to express regex → 1 line change
2. **BUG 3** — Add `x-unresolved: True` to unresolvable schemas → 4 line change
3. **BUG 2** — Take shortest `app.use()` match → ~5 line change

**Estimated combined impact:** EP-F1 +0.05, SC-F1 precision improvement (cleaner scoring)
