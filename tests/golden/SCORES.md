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
