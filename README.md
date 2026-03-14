# Swagger Agent

A multi-agent system that generates OpenAPI 3.0 specifications from arbitrary codebases. Point it at a project directory and it produces a spec complete enough for a penetration tester to use as an attack surface map.

## What it does

- Analyzes any web application codebase (Spring Boot, FastAPI, Express, ASP.NET, Laravel, Go, Rust, serverless, etc.)
- Extracts all HTTP endpoints with methods, paths, parameters, request bodies, and response codes
- Identifies authentication schemes and per-endpoint security requirements
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

Private / Internal use.
