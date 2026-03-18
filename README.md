# Swagger Agent

A multi-agent system that generates OpenAPI 3.0 specifications from arbitrary codebases. Point it at a project directory and it produces a spec complete enough for a penetration tester to use as an attack surface map.

## What it does

- Analyzes any web application codebase (Spring Boot, FastAPI, Express, ASP.NET, Laravel, Go, Rust, serverless, etc.)
- Extracts all HTTP endpoints with methods, paths, parameters, request bodies, and response codes
- Identifies authentication schemes (OAuth2 with flow types and scopes, Bearer, API key, Basic, Cookie) with per-endpoint security requirements
- Extracts schema definitions with validation constraints from model files
- Outputs a valid OpenAPI 3.0 YAML spec

## Quick start

```bash
# Install
pip install -e .

# Configure LLM (see Configuration below)
cp .env.example .env  # edit with your LLM settings

# Run against a local codebase
python -m swagger_agent /path/to/your/project

# Run against a GitHub/GitLab repo
python -m swagger_agent https://github.com/owner/repo
python -m swagger_agent owner/repo              # GitHub shorthand
python -m swagger_agent owner/repo --ref v2.0    # specific branch/tag/commit

# Output goes to outputs/<project-name>/openapi.yaml
```

### Requirements

- Python 3.11+
- [Universal Ctags](https://ctags.io/) (`brew install universal-ctags` / `apt install universal-ctags`)
- An OpenAI-compatible LLM API endpoint

### Docker

No Python or ctags installation needed — everything is bundled in the image.

```bash
# Build the image
docker build -t swagger-agent .

# Scan a local repo
docker run --rm \
  -v /path/to/project:/work \
  -v swagger-cache:/app/.cache \
  -v ./outputs:/app/outputs \
  --env-file .env \
  swagger-agent /work

# Scan a remote repo
docker run --rm \
  -v swagger-cache:/app/.cache \
  -v ./outputs:/app/outputs \
  --env-file .env \
  swagger-agent owner/repo

# Score results against golden test data
docker run --rm \
  -v ./outputs:/app/outputs \
  --entrypoint python \
  swagger-agent tests/golden/score.py /app/outputs/
```

Or use Docker Compose:

```bash
# Scan a local repo
TARGET_DIR=/path/to/project docker compose run --rm swagger-agent

# Scan a remote repo
docker compose run --rm swagger-agent python -m swagger_agent owner/repo
```

The `swagger-cache` named volume persists LLM response cache across runs, avoiding redundant API calls. Configure your LLM backend in `.env` (copy from `.env.example`).

### Webhook Server

Run as an HTTP service that accepts repo URLs and returns generated specs:

```bash
# Start the server
docker compose up swagger-server

# Or without Docker
pip install -e '.[server]'
uvicorn swagger_agent.server:app --host 0.0.0.0 --port 8000
```

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/generate` | Submit a job, returns `202` with `{job_id, status}` immediately |
| `GET` | `/jobs/{id}` | Poll job status and live progress. Returns spec/yaml/timings when done |
| `GET` | `/jobs/{id}/yaml` | Download gzipped YAML (only when job is done) |
| `GET` | `/health` | Health check |

**Request body** (for `POST /generate`):

```json
{
  "repo_url": "https://github.com/owner/repo.git",
  "branch": "develop",
  "tag": "",
  "commit": "",
  "token": ""
}
```

- `branch`, `tag`, `commit` — pin to an exact ref (only one allowed per request). Branch and tag use shallow clone (fast). Commit requires a full clone.
- `token` — git auth token for private repos (injected into HTTPS URL)

**Job statuses:** `pending` → `cloning` → `running` → `done` (or `failed`)

**Private repo auth** (in order of priority):

1. Per-request `token` field
2. `GIT_TOKEN` env var (set in `.env` or docker compose)
3. Host SSH keys (mount `~/.ssh` — see commented-out line in docker-compose.yml)

**Examples:**

```bash
# Submit a job
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/owner/repo.git"}'
# → {"job_id": "a1b2c3d4", "status": "pending"}

# Poll for status and progress
curl http://localhost:8000/jobs/a1b2c3d4
# → {"job_id": "a1b2c3d4", "status": "running", "progress": {
#      "phase": "Route Extraction", "routes_done": 1, "routes_total": 3,
#      "endpoints_found": 4, "schemas_resolved": 0,
#      "log": ["Cloned in 650ms", "Phase 1: Scout", "Extracting users.py (1/3)", ...]
#    }}
# When done: includes "spec", "yaml", "timings" fields

# Specific tag
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/owner/repo.git", "tag": "v2.1.0"}'

# Private repo with token
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/org/private-repo.git", "token": "ghp_xxxx"}'

# Download gzipped YAML when done
curl http://localhost:8000/jobs/a1b2c3d4/yaml --output openapi.yaml.gz
```

## How it works

The system has two layers: **LLM-powered agents** that interpret source code, and **deterministic infrastructure** that manages artifacts, resolves references, assembles the spec, and validates it.

```
                    ┌─────────────┐
                    │ Orchestrator│
                    └──────┬──────┘
                           │ delegates
              ┌────────────┼────────────┐
              ▼            ▼            │
         ┌────────┐  ┌───────────┐     │
         │ Scout  │  │  Route    │     │
         │        │  │ Extractor │     │
         └───┬────┘  └─────┬─────┘     │
             │              │           │
             ▼              ▼           │
        ┌─────────────────────────┐     │
        │   Infrastructure        │     │
        │  ┌──────────────────┐   │     │
        │  │  Artifact Store  │   │     │
        │  │  Ref Resolver    │   │     │
        │  │  Assembler       │──────►  YAML
        │  │  Validator       │   │
        │  └────────┬─────────┘   │
        │           │             │
        │           ▼             │
        │    ┌──────────────┐     │
        │    │   Schema     │◄────┘
        │    │  Extractor   │
        │    └──────────────┘
        └─────────────────────────┘
```

### Agents

| Agent | Role |
|-------|------|
| **Scout** | Explores the project to identify framework, route files, and server URLs |
| **Route Extractor** | Reads a single route file and extracts all endpoint metadata (two-phase: code analysis then extraction) |
| **Schema Extractor** | Reads a model file and extracts JSON Schema definitions with validation constraints |
| **Orchestrator** | Reads a state summary and decides what to extract next |

### Infrastructure

| Module | Role |
|--------|------|
| **Artifact Store** | Persists all JSON artifacts on disk |
| **Ref Resolver** | Resolves type references from endpoints to model files via ctags + import parsing |
| **Assembler** | Builds OpenAPI YAML from artifacts |
| **Validator** | Runs OpenAPI spec validation |
| **Completeness Checker** | Evaluates the spec against a quality checklist |
| **Ctags Prefilter** | Strips function bodies from route files before Phase 2 extraction to reduce LLM input |

## Configuration

All configuration is via environment variables or a `.env` file:

```bash
# LLM endpoint (any OpenAI-compatible API)
LLM_BASE_URL=http://localhost:8080/v1
LLM_MODEL=your-model-name
LLM_API_KEY=your-key

# Instructor mode for structured output
INSTRUCTOR_MODE=tools  # tools, json, json_schema, md_json, openrouter_structured_outputs

# Per-agent model overrides (optional)
LLM_MODEL_SCOUT=
LLM_MODEL_ROUTE_EXTRACTOR=
LLM_MODEL_SCHEMA_EXTRACTOR=

# Per-agent base URL overrides (optional)
LLM_BASE_URL_SCOUT=
LLM_BASE_URL_ROUTE_EXTRACTOR=
LLM_BASE_URL_SCHEMA_EXTRACTOR=

# Concurrency (default: 1 = sequential)
MAX_WORKERS_ROUTE=3
MAX_WORKERS_SCHEMA=3
```

## CLI usage

```bash
# Local project
python -m swagger_agent /path/to/project

# Remote repo (cloned to /tmp/swagger-agent/<repo>)
python -m swagger_agent https://github.com/owner/repo
python -m swagger_agent owner/repo                   # GitHub shorthand
python -m swagger_agent git@gitlab.com:org/repo.git   # SSH URL
python -m swagger_agent owner/repo --ref develop      # branch/tag/commit

# Custom output path
python -m swagger_agent /path/to/project -o spec.yaml

# Cache control (cache is ON by default)
python -m swagger_agent /path/to/project --no-cache         # skip cache entirely
python -m swagger_agent /path/to/project --overwrite-cache  # re-run LLM calls, update cache
python -m swagger_agent --clear-cache                       # wipe all cached responses

# Show per-LLM-call telemetry
python -m swagger_agent /path/to/project --telemetry

# Replay telemetry from a previous run
python -m swagger_agent --telemetry-from outputs/my-project/result.json

# Skip the Scout agent (use deterministic prescan only)
python -m swagger_agent /path/to/project --skip-scout

# Disable live dashboard
python -m swagger_agent /path/to/project --no-dashboard
```

## Output

Each run produces two files in `outputs/<project-name>/`:

- **`openapi.yaml`** -- The generated OpenAPI 3.0 spec
- **`result.json`** -- Full pipeline result including discovery manifest, endpoint descriptors, schemas, completeness checklist, validation results, and per-call LLM telemetry

## Project structure

```
swagger_agent/
  agents/
    scout/          # Project discovery agent (stateless turn loop)
    route_extractor/# Two-phase endpoint extraction agent
    schema_extractor/# Model/schema extraction agent
  infra/
    assembler.py    # OpenAPI YAML assembly
    resolve.py      # Type resolution via ctags + grep
    schema_loop.py  # Infrastructure-driven schema extraction loop
    ctags_filter.py # Ctags-based route file prefiltering
    validator.py    # OpenAPI spec validation
    detectors/      # Deterministic route/framework detection (prescan)
  config.py         # LLM configuration (env vars / .env)
  models.py         # Pydantic models for all artifacts
  pipeline.py       # Main pipeline orchestration
  server.py         # FastAPI webhook server
  telemetry.py      # Per-call LLM metrics collection
  dashboard.py      # Rich live terminal dashboard
tests/
  e2e/repos/        # Test repositories (various frameworks)
```

## Design principles

- **Framework-agnostic**: Works on any codebase. No framework dispatch tables or language-specific heuristics in infrastructure. The LLM figures out the framework; infrastructure operates on universal code structure.
- **Agents produce JSON, infrastructure produces YAML**: This boundary is absolute. Agents never build specs, infrastructure never interprets source code.
- **Lazy schema extraction**: Only extracts models reachable from route type references. Never scans the full model tree.
- **Working output over perfect output**: Emits what it can extract, uses reasonable defaults for what it can't. Never blocks on missing information.

## License

All rights reserved. Source code is publicly viewable but may not be used, copied, modified, or distributed without explicit permission from the author.
