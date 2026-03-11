# Data Models

All artifacts and state objects are Pydantic v2 models. They serve as the contract between agents (LLM layer) and infrastructure (deterministic layer). Agents produce them via `write_artifact`, infrastructure validates/stores/assembles from them.

## Ref Hints

Every schema reference in an endpoint descriptor is a `RefHint`, not a bare string. This gives infrastructure enough information to resolve the reference deterministically.

```python
class RefHint(BaseModel):
    ref_hint: str
    import_source: str | None = None
    resolution: Literal["import", "class_to_file", "unresolvable"]
```

| Field | Purpose |
|-------|---------|
| `ref_hint` | The type name as it appears in code (e.g. `UserResponse`) |
| `import_source` | Raw import line (e.g. `from app.schemas.user import UserResponse`). Present when `resolution` is `"import"`. |
| `resolution` | How infrastructure should resolve this ref. See CLAUDE.md for the resolution algorithm. |

## Discovery Manifest

Scout agent output. One per run.

```python
class SecurityScheme(BaseModel):
    name: str
    type: Literal["http", "apiKey", "oauth2", "openIdConnect"]
    scheme: str | None = None
    bearer_format: str | None = None
    in_: Literal["header", "query", "cookie"] | None = Field(None, alias="in")
    source_file: str | None = None

class ErrorModel(BaseModel):
    name: str
    source_file: str | None = None

class DiscoveryManifest(BaseModel):
    framework: str
    language: str
    entry_points: list[str] = []
    route_files: list[str] = []
    model_files: list[str] = []
    security_schemes: list[SecurityScheme] = []
    servers: list[str] = []
    base_path: str = ""
    error_models: list[ErrorModel] = []
    cors_config: dict | None = None
    dependency_graph: dict[str, list[str]] = {}
    class_to_file: dict[str, str] = {}
```

### Field notes

- `dependency_graph`: maps each file to the list of files it imports. Used by Ref Resolver to walk transitive dependencies.
- `class_to_file`: maps class/model names to the file that defines them. Fallback resolution when imports are unavailable.
- `servers`: raw URLs (e.g. `["http://localhost:8000"]`). Infrastructure wraps these into OpenAPI `servers` objects.
- `error_models`: named error shapes found in global handlers. Injected into Route Extractor context so it can reference them in error responses.

## Endpoint Descriptor

Route Extractor agent output. One per route file.

```python
class Parameter(BaseModel):
    name: str
    in_: Literal["path", "query", "header", "cookie"] = Field(alias="in")
    required: bool = False
    schema_: dict = Field(default_factory=dict, alias="schema")

class RequestBody(BaseModel):
    content_type: str = "application/json"
    schema_ref: RefHint | None = None

class Response(BaseModel):
    status_code: str
    description: str = ""
    schema_ref: RefHint | None = None

class Endpoint(BaseModel):
    method: str
    path: str
    operation_id: str
    tags: list[str] = []
    security: list[str] | None = None
    parameters: list[Parameter] = []
    request_body: RequestBody | None = None
    responses: list[Response] = []

class EndpointDescriptor(BaseModel):
    source_file: str
    endpoints: list[Endpoint]
```

### Field notes

- `security`: `None` means inherit global auth. `[]` (empty list) means explicitly public. `["BearerAuth"]` means that specific scheme is required.
- `content_type`: defaults to `application/json`. File uploads use `multipart/form-data` with `format: binary` in the schema.
- `schema_` uses an alias because `schema` is a reserved Pydantic namespace. Serializes as `"schema"` in JSON output.
- `status_code` is a string to support patterns like `"2XX"` or `"default"`.

## Schema Descriptor

Schema Extractor agent output. One per model file.

```python
class SchemaDescriptor(BaseModel):
    source_file: str
    schemas: dict[str, dict]
```

`schemas` maps model names to JSON Schema objects. Example:

```json
{
  "source_file": "app/models/user.py",
  "schemas": {
    "User": {
      "type": "object",
      "required": ["username", "email"],
      "properties": {
        "username": {"type": "string", "minLength": 1, "maxLength": 50},
        "email": {"type": "string", "format": "email"},
        "bio": {"type": "string", "nullable": true},
        "image": {"type": "string", "format": "uri", "nullable": true}
      }
    },
    "UserResponse": {
      "type": "object",
      "required": ["user"],
      "properties": {
        "user": {"$ref": "#/components/schemas/User"}
      }
    }
  }
}
```

References to other schemas use `$ref` with the full OpenAPI path. Circular references are always expressed as `$ref` — never inlined.

## State Models

Used by infrastructure to communicate pipeline status to the orchestrator.

```python
class Phase(str, Enum):
    INIT = "init"
    SCOUTING = "scouting"
    EXTRACTING_ROUTES = "extracting_routes"
    AWAITING_SCHEMAS = "awaiting_schemas"
    DONE = "done"

class CompletenessChecklist(BaseModel):
    has_endpoints: bool = False
    has_security_schemes: bool = False
    endpoints_have_auth: bool = False
    has_error_responses: bool = False
    has_request_bodies: bool = False
    has_schemas: bool = False
    no_unresolved_refs: bool = False
    has_servers: bool = False
    route_coverage: float = 0.0

class RoutesStatus(BaseModel):
    total: int = 0
    extracted: int = 0
    pending: list[str] = []

class StateSummary(BaseModel):
    phase: Phase = Phase.INIT
    framework: str = ""
    routes: RoutesStatus = RoutesStatus()
    schemas_complete: bool = False
    completeness: CompletenessChecklist = CompletenessChecklist()
    validation_errors_summary: str = ""
    retry_count: int = 0
```

The orchestrator only ever sees `StateSummary`. It is produced by the State Producer module and injected into the orchestrator's context. The orchestrator never constructs or modifies it.

## Scout Working State

Internal to the Scout agent's single invocation. Not a shared artifact.

```python
class ScoutWorkingState(BaseModel):
    """Re-injected at every ReAct step to prevent context loss."""
    framework: str | None = None
    language: str | None = None
    entry_points: list[str] = []
    route_files: list[str] = []
    model_files: list[str] = []
    security_schemes: list[SecurityScheme] = []
    servers: list[str] = []
    base_path: str = ""
    error_models: list[ErrorModel] = []
    dependency_graph: dict[str, list[str]] = {}
    class_to_file: dict[str, str] = {}
    scratchpad: str = ""
    remaining_tasks: list[str] = [
        "identify_framework",
        "find_entry_points",
        "find_route_files",
        "find_model_files",
        "identify_security",
        "find_servers",
        "find_error_handlers",
        "build_dependency_graph",
        "build_class_to_file_map",
    ]
```

When the Scout calls `update_state(updates)`, the fields in `updates` are merged into this object. At the end, `write_artifact("discovery_manifest", ...)` serializes the accumulated state into a `DiscoveryManifest` — no lossy summarization.

## Serialization Conventions

- All models use `model_dump(by_alias=True)` for JSON output so aliased fields (`in_` → `in`, `schema_` → `schema`) serialize correctly.
- Artifact store writes and reads use `model_validate()` / `model_dump()`.
- `None` fields are excluded from serialized output (`model_dump(exclude_none=True)`) to keep artifacts compact.
