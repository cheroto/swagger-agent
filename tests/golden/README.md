# Golden Test Data

Manually curated ground truth for evaluating pipeline output quality.

## Format

Each `<repo>.json` file contains:
- `endpoints`: Expected HTTP endpoints `[{method, path, has_auth}]`
  - `path` uses OpenAPI `{param}` syntax
  - `has_auth` is `true` if the endpoint requires authentication, `false` if public
- `schemas`: Expected schema names that should be extracted `[string]`
- `security_schemes`: Expected security scheme types `[{name, type}]`
  - `type` is the OpenAPI securitySchemes type: `http`, `apiKey`, `oauth2`

## Scoring

F1 scores are computed separately for endpoints and schemas:
- **Endpoint match**: `(method, normalized_path)` tuple equality
- **Schema match**: case-insensitive name equality

Run `python tests/golden/score.py /tmp/swagger-test/` to compute F1 scores.
