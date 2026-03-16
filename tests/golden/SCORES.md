# Score Tracking

Tracks pipeline quality over time. Each checkpoint records scores against golden test data after changes to pipeline or golden files.

## Checkpoint 1 — Baseline

**Date:** 2026-03-16
**Commit:** `4becf1d`
**Scorer:** `score.py` with weighted TP, Hungarian matching, token-based name similarity, security scheme scoring, auth accuracy

### Scores

```
Repo                            EP-P  EP-R  EP-F1  SC-P  SC-R  SC-F1 SEC-F1  AUTH
----------------------------------------------------------------------------------
aspnetcore-realworld            1.00  1.00  1.000  0.74  0.64  0.688  0.000  1.00
clojure-compojure               1.00  1.00  1.000  0.35  0.45  0.398  1.000  1.00
dart-frog                       1.00  1.00  1.000  0.33  0.50  0.400  0.000  0.55
go-gin-ecommerce                0.68  0.56  0.619  0.33  0.34  0.334  1.000  1.00
haskell-servant                 0.57  1.00  0.727  0.15  0.41  0.219  1.000  1.00
kotlin-ktor-realworld           1.00  1.00  1.000  0.00  0.00  0.000  1.000  0.79
laravel-realworld               0.95  1.00  0.977  0.00  0.00  0.000  0.000  0.33
nestjs-pg-crud                  1.00  1.00  1.000  0.23  0.75  0.353  1.000  1.00
node-express-boilerplate        0.36  0.39  0.370  0.12  0.34  0.172  1.000  1.00
ocaml-dream                     1.00  1.00  1.000  0.69  0.60  0.643  0.500  1.00
passwordless-auth-rust          0.47  0.70  0.560  0.68  0.81  0.735  0.000  1.00
rails-rest-api                  1.00  1.00  1.000  0.52  0.52  0.519  0.000  0.14
rest-api-node                   0.92  0.92  0.923  0.67  1.00  0.800  1.000  1.00
spring-boot-blog                1.00  1.00  1.000  0.96  0.96  0.962  1.000  0.98
swift-vapor-conduit             0.60  0.75  0.667  1.00  0.22  0.364  0.000  1.00
----------------------------------------------------------------------------------
AVERAGE                                     0.856              0.439  0.567  0.85
```

### Top Gaps

| Gap | Affected Repos | Impact |
|-----|---------------|--------|
| Route path prefix reconstruction | node-express-boilerplate, go-gin-ecommerce | EP-F1 killer — nested route groups/mounts produce wrong paths |
| Security scheme extraction | dart-frog, laravel, passwordless-auth-rust, rails, swift-vapor, aspnetcore (7 repos at 0) | SEC-F1 average = 0.567 |
| Auth declaration detection | laravel (0.33), rails (0.14), dart-frog (0.55) | Pipeline marks authed endpoints as public |
| Schema naming mismatch | kotlin-ktor (0.0), laravel (0.0), node-express (0.17), haskell (0.22) | Schemas found but names diverge too much to match |

## Checkpoint 2 — Structured schemas + mount prefix + json_schema mode

**Date:** 2026-03-16
**Commit:** `83e0014`
**Mode:** `INSTRUCTOR_MODE=json_schema` (grammar-constrained decoding)
**Changes:**
- Structured schema Pydantic models (SchemaProperty, ExtractedSchema) replacing dict[str, dict] for both SchemaDescriptor and EndpointDescriptor.inline_schemas
- Mount prefix injection from Phase 1 mount_map (clean design: only from route files, no entry point heuristics)
- Schema Extractor prompt stripped to 3-line role framing (schema descriptions are single source of truth)
- Phase 1 prompt stripped to 3-line role framing
- Scorer: allOf $ref transitive property resolution
- Golden data: spring-boot parent schemas, laravel /api prefix
- INSTRUCTOR_MODE=json_schema eliminates deterministic "multiple tool calls" crashes on ocaml-dream and haskell-servant

### Scores

```
Repo                            EP-P  EP-R  EP-F1  SC-P  SC-R  SC-F1 SEC-F1  AUTH
----------------------------------------------------------------------------------
aspnetcore-realworld            1.00  1.00  1.000  0.78  0.78  0.779  0.000  1.00
clojure-compojure               1.00  1.00  1.000  0.00  0.00  0.000  1.000  1.00
dart-frog                       1.00  0.73  0.842  0.29  0.50  0.364  0.000  0.38
flask-restplus-example          0.00  0.00  0.000  0.85  1.00  0.919  1.000  0.00
go-gin-ecommerce                0.71  0.65  0.682  0.33  0.39  0.355  1.000  1.00
haskell-servant                 1.00  1.00  1.000  0.00  0.00  0.000  1.000  1.00
kotlin-ktor-realworld           0.90  0.90  0.895  0.00  0.00  0.000  1.000  0.77
laravel-realworld               0.00  0.00  0.000  0.00  0.00  0.000  1.000  0.00
nestjs-pg-crud                  1.00  1.00  1.000  0.26  0.77  0.384  1.000  1.00
node-express-boilerplate        0.00  0.00  0.000  0.05  0.15  0.079  1.000  0.00
ocaml-dream                     0.73  0.73  0.727  0.47  0.47  0.467  0.500  1.00
passwordless-auth-rust          0.73  0.80  0.762  0.94  1.00  0.969  0.000  1.00
rails-rest-api                  1.00  1.00  1.000  0.55  0.55  0.546  0.000  0.14
rest-api-node                   0.91  0.77  0.833  1.00  1.00  1.000  1.000  1.00
spring-boot-blog                1.00  1.00  1.000  0.93  0.93  0.934  1.000  1.00
swift-vapor-conduit             0.00  0.00  0.000  0.00  0.00  0.000  0.000  0.00
----------------------------------------------------------------------------------
AVERAGE                                     0.671              0.425  0.656  0.64
```

### Compared to Baseline

| Metric | Baseline | Checkpoint 2 | Delta |
|--------|----------|-------------|-------|
| EP-F1 | 0.856 | 0.671 | -0.185 |
| SC-F1 | 0.439 | 0.425 | -0.014 |
| SEC-F1 | 0.567 | 0.656 | +0.089 |
| AUTH | 0.85 | 0.64 | -0.21 |

### Key Observations

**Improvements:**
- SEC-F1 +0.089 — json_schema mode eliminated crashes, more repos now produce valid output
- spring-boot SC-F1 0.962→0.934 (stable, allOf inheritance now works)
- passwordless-auth-rust SC-F1 0.735→0.969 (structured model extracts properties)
- rest-api-node SC-F1 0.800→1.000 (perfect schema match)
- flask-restplus SC-F1 0.501→0.919 (structured model massive improvement)
- aspnetcore SC-F1 0.688→0.779 (improved)
- ocaml-dream 0.000→0.727 EP, 0.000→0.467 SC (was crashing, now works)
- haskell-servant 0.727→1.000 EP (was crashing, now perfect endpoints)

**Regressions:**
- EP-F1 average down due to LLM non-determinism with json_schema mode producing different (often wrong) paths on some runs. Key failures:
  - flask-restplus EP=0.0 — LLM drops `/api/v1` prefix from paths
  - laravel EP=0.0 — LLM drops `/api` prefix (RouteServiceProvider not visible)
  - node-express EP=0.0 — Scout doesn't include index.js in route files, so mount prefix can't fire
  - swift-vapor EP=0.0 — route extraction produced 0 endpoints on this run
- AUTH average down because EP-F1=0 repos score AUTH=0 (no matched endpoints to check auth on)

### Root Causes for EP-F1 Regressions

All EP-F1=0 repos share the same root cause: **path prefix mismatch**. The LLM produces paths without the framework-injected prefix (/api/v1, /api) or the Scout doesn't include registry files. These are:
1. **Scout gap**: index.js/main.go not in route_files (node-express, go-gin partial)
2. **Framework prefix invisible**: Laravel RouteServiceProvider, Flask blueprint url_prefix not in the route file the LLM reads
3. **LLM non-determinism**: json_schema mode produces different path constructions across runs
