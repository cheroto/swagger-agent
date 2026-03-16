from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# --- Ref Hints ---


class RefHint(BaseModel):
    ref_hint: str = Field(description="Type name EXACTLY as it appears in code (class name, struct name, interface name). Use the inner type only, strip collection wrappers: List<Article> → 'Article'. Do NOT invent names — if the response is constructed inline (e.g., res.json({...}), gin.H{}, render json: {...}) with no named type, set resolution='unresolvable' and use a descriptive placeholder.")
    resolution: Literal["import", "class_to_file", "unresolvable"] = Field(description="'import' = found the EXACT import/require/using statement for this type — provide import_line. 'class_to_file' = same namespace/package, no explicit import needed, but the type IS a real named class/struct/interface in the codebase. 'unresolvable' = no named type exists: framework builtins (Response, IActionResult, int, string), external package types, OR inline/anonymous response shapes with no class definition.")
    import_line: str = Field(default="", description="The exact import/require/using statement. Only meaningful when resolution='import'. Empty string otherwise.")
    file_namespace: str = Field(default="", description="The namespace/package/module declaration of the current file (e.g. 'namespace Conduit.Features.Articles;', 'package com.example.users;'). Helps disambiguate same-name types across packages.")
    is_array: bool = Field(default=False, description="True if the original type was a collection/list/array wrapper (List<T>, T[], IEnumerable<T>). ref_hint contains the inner element type only.")
    is_nullable: bool = Field(default=False, description="True if the original type was optional/nullable (Optional[T], T?, T | null). ref_hint contains the inner type only.")

    @property
    def is_empty(self) -> bool:
        """True when ref_hint is blank — no meaningful type reference."""
        return not self.ref_hint


# --- Discovery Manifest ---


class SecurityScheme(BaseModel):
    name: str
    type: Literal["http", "apiKey", "oauth2", "openIdConnect"]
    scheme: str | None = None
    bearer_format: str | None = None
    in_: Literal["header", "query", "cookie"] | None = Field(None, alias="in")
    source_file: str | None = None

    model_config = {"populate_by_name": True}


class ErrorModel(BaseModel):
    name: str
    source_file: str | None = None


class DiscoveryManifest(BaseModel):
    framework: str
    language: str
    route_files: list[str] = []
    servers: list[str] = []
    base_path: str = ""
    default_auth_hint: str = Field(default="", description="Project-wide auth mechanism detected in base controllers, middleware configs, or security filters. Describes how auth is applied globally so the Route Extractor can infer per-endpoint auth even when the route file has no auth markers. Empty if no global auth detected.")


# --- Code Analysis (Route Extractor Phase 1) ---


class EndpointSketch(BaseModel):
    """Lightweight endpoint identification from Phase 1."""
    method: str = Field(description="HTTP method: GET, POST, PUT, PATCH, DELETE.")
    path: str = Field(description="Full path as observed in code, including any router prefix.")
    handler_name: str = Field(description="The function or method name that handles this endpoint.")


class AuthPattern(BaseModel):
    """Observed auth mechanism from Phase 1."""
    mechanism: str = Field(description="How auth is applied: 'middleware in handler chain', 'decorator', 'annotation', 'dependency injection'.")
    indicator: str = Field(description="The exact code marker, e.g. 'auth.required', '@PreAuthorize', '[Authorize]', 'Depends(get_current_user)'.")
    scheme_type: str = Field(description="Auth type: 'bearer', 'apikey', 'cookie', 'basic', 'oauth2', 'unknown'. Prefer the type explicitly declared in the source code (e.g., SecuritySchemeType.ApiKey → 'apikey', cookie_sessions → 'cookie') over inference from token format.")
    applies_to: str = Field(description="Scope: 'all' (class/router level), 'per-endpoint', 'group' (middleware group).")


class CodeAnalysis(BaseModel):
    """Phase 1 output: observations about the route file."""
    routing_style: str = Field(description="How routes are defined, e.g. 'decorator-based', 'method chaining', 'attribute routing'.")
    path_param_syntax: str = Field(description="Path parameter syntax used: ':param', '{param}', or '<param>'.")
    base_prefix: str = Field(description="Router/controller-level path prefix if any, e.g. '/api/posts', '/articles'. Empty string if none.")
    auth_patterns: list[AuthPattern] = Field(description="All auth mechanisms observed. Empty list if no auth patterns found.")
    has_auth_imports: bool = Field(description="Whether auth-related imports exist in the file.")
    auth_inference_notes: str = Field(default="", description="When no explicit auth patterns found: note indirect signals like middleware groups, comments, or naming conventions suggesting auth is applied externally.")
    request_body_style: str = Field(description="How request bodies are consumed, e.g. 'req.body', '@RequestBody', 'FromBody', 'Depends'.")
    error_handling_notes: str = Field(description="How errors are returned: try/catch patterns, error middleware, custom error classes.")
    import_lines: list[str] = Field(description="All import/require/using lines from the file, copied verbatim.")
    endpoints: list[EndpointSketch] = Field(description="Every endpoint found in the file.")
    mount_map: dict[str, str] = Field(default_factory=dict, description="When this file mounts sub-routers/controllers at URL path prefixes, map the sub-file identifier to the URL path prefix. Keys: filename, module path, or function name. Values: MUST be URL path segments starting with '/' (e.g., '/auth', '/users', '/api/v1'). Do NOT use type names, class names, or module names as values — only URL string literals from the code. Examples: {'auth.route': '/auth', 'user.route': '/users'}. Empty dict if this file defines endpoints directly rather than mounting sub-routers, or if the URL prefix cannot be determined from string literals.")


# --- Security ---


class SecurityRequirement(BaseModel):
    name: Literal["BearerAuth", "ApiKeyAuth", "BasicAuth", "OAuth2", "CookieAuth"] = Field(description="Security scheme name. Must match the auth mechanism: BearerAuth for JWT/token, ApiKeyAuth for API keys, BasicAuth for HTTP Basic, OAuth2 for OAuth2 flows, CookieAuth for cookie/session auth.")
    scheme_type: Literal["bearer", "apikey", "basic", "oauth2", "cookie"] = Field(description="Auth mechanism: 'bearer' for JWT/token in Authorization header, 'apikey' for API key in header/query, 'basic' for HTTP Basic, 'oauth2' for OAuth2 flows, 'cookie' for cookie-based session auth.")


# --- Endpoint Descriptor ---


class Parameter(BaseModel):
    name: str = Field(description="Parameter name as it appears in the code.")
    in_: Literal["path", "query", "header", "cookie"] = Field(alias="in", description="Parameter location.")
    required: bool = Field(default=False, description="True for path params (always required) and any explicitly required query/header params.")
    schema_: dict = Field(default_factory=dict, alias="schema", description="JSON Schema for the parameter type, e.g. {'type': 'string'}, {'type': 'integer', 'format': 'int64'}.")

    model_config = {"populate_by_name": True}


class RequestBody(BaseModel):
    content_type: str = Field(default="application/json", description="'application/json' for structured data, 'multipart/form-data' for file uploads, 'application/x-www-form-urlencoded' for form data.")
    schema_ref: RefHint = Field(description="Type reference for the request body. Always provide — use resolution 'unresolvable' with a descriptive name if the type cannot be determined.")


class Response(BaseModel):
    status_code: str = Field(description="HTTP status code: '200', '201', '401', '403', '404', '422'. POST that creates → 201, DELETE → 200/204, GET/PUT/PATCH → 200. Auth endpoints must have 401. Path param endpoints should have 404.")
    description: str = ""
    schema_ref: RefHint = Field(description="Type reference for the response body. Always provide — use resolution='unresolvable' with a descriptive ref_hint for the response type. For bodyless responses (204, 401 with no detail), use ref_hint='' with resolution='unresolvable'.")


class Endpoint(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = Field(description="HTTP method (uppercase). Only standard HTTP methods — do not include WebSocket, gRPC, or other non-HTTP protocols.")
    path: str = Field(description="Full path including base_path. Use OpenAPI parameter syntax {param} — convert from framework syntax (:param → {param}, <param> → {param}, <int:param> → {param}). Strip route constraints, optional markers, catch-all prefixes.")
    operation_id: str = Field(description="Derived from the handler function name (e.g. 'getUser', 'create_article').")
    tags: list[str] = Field(default_factory=list, description="Grouping tags derived from controller/router name, e.g. ['Articles'].")
    security: list[SecurityRequirement] = Field(default_factory=list, description="Security requirements. [] = explicitly public (no auth). Always set, never omit. Determine auth from per-endpoint markers in the code (decorators, middleware, guards). When a global auth context is provided but no per-endpoint markers exist: set auth on state-changing endpoints (POST/PUT/PATCH/DELETE on resources) and private data access; set [] on login/register/password-reset, health/docs/version, and public read-only listings. Endpoints with explicit skip/exclude/AllowAnonymous/permitAll markers are always public. When an auth wrapper has an 'optional' flag (e.g. optional=true), treat as public (security: []). When in doubt, prefer [] (public).")
    parameters: list[Parameter] = Field(default_factory=list)
    request_body: RequestBody | None = Field(default=None, description="Include for any endpoint that consumes a request body. For POST/PUT/PATCH with unknown body shape, provide with schema_ref resolution='unresolvable'. Set null ONLY for bodyless endpoints (GET, DELETE, state toggles).")
    request_body_reason: str = Field(default="", description="When request_body is null on POST/PUT/PATCH, explain why: 'state toggle', 'action endpoint', 'webhook callback', 'no body evidence in code'. Empty string when request_body is provided or method is GET/DELETE.")
    responses: list[Response] = Field(default_factory=list, description="All response codes including errors. Auth endpoints must have 401. Endpoints with path params should have 404.")


class EndpointDescriptor(BaseModel):
    source_file: str
    endpoints: list[Endpoint]
    inline_schemas: list[ExtractedSchema] = Field(default_factory=list, description="Schemas for types whose shape is visible in this file's validation code but have no importable class definition (e.g., inline Pydantic models, anonymous types, validation schemas). Infrastructure merges these into the schema store before resolution.")

    def inline_schemas_as_dict(self) -> dict[str, dict]:
        """Convert inline_schemas to JSON Schema dict format for downstream consumption."""
        result = {}
        for schema in self.inline_schemas:
            result[schema.name] = _extracted_to_json_schema(schema)
        return result


# --- Schema Descriptor ---


class SchemaProperty(BaseModel):
    """A single property on a data model / DTO / entity."""
    name: str = Field(description="Property name as serialized in JSON. Use the alias if a serialization annotation provides one (@JsonProperty, @SerializedName, alias=, CodingKeys). Skip fields with exclusion annotations (@JsonIgnore, [JsonIgnore], @Transient, @Expose(serialize:false)).")
    type: str = Field(description="JSON Schema type: 'string', 'integer', 'number', 'boolean', 'array', 'object'. Use the base type even for references (use 'object' and set ref).")
    format: str = Field(default="", description="JSON Schema format when applicable: 'date-time', 'date', 'email', 'uuid', 'uri', 'binary', 'int64', 'float', 'double'. Empty string if none.")
    ref: str = Field(default="", description="Referenced schema name when this property is a complex type (another model/DTO). Empty for primitives. Use the class name only, not a full $ref path.")
    is_array: bool = Field(default=False, description="True if this property is a list/array/set of the type (List<User> → type='object', ref='User', is_array=True).")
    nullable: bool = Field(default=False, description="True if this property accepts null/None/nil (Optional<T>, T?, T | null).")
    enum_values: list[str] = Field(default_factory=list, description="Enum values if this is an enum type, e.g. ['active', 'inactive', 'banned']. Empty list if not an enum.")
    constraints: dict[str, object] = Field(default_factory=dict, description="Validation constraints from code annotations/decorators/validators. Use standard JSON Schema keywords: minLength, maxLength, pattern, minimum, maximum, exclusiveMinimum, exclusiveMaximum, multipleOf, minItems, maxItems, uniqueItems. Example: {'minLength': 3, 'maxLength': 50, 'pattern': '^[a-zA-Z]+$'}. Empty dict if no constraints.")


class ExtractedSchema(BaseModel):
    """A single model / DTO / entity extracted from source code."""
    name: str = Field(description="Class/struct/record/model name exactly as defined in code. Preserve original casing.")
    properties: list[SchemaProperty] = Field(description="ALL data-carrying fields on this model. Every model has at least one field — if you cannot identify fields, the type is likely not a data model. Include inherited fields unless the parent is in known_schemas (then use parent_ref instead).")
    required_fields: list[str] = Field(default_factory=list, description="Names of fields that are mandatory: no default value, not optional, or annotated with not-null/not-blank validators. Empty list if all fields are optional.")
    parent_ref: str = Field(default="", description="Parent class/struct name if this model inherits from a type in known_schemas. Empty string if no inheritance or parent is not in known_schemas (in that case, include inherited fields in properties).")


class SchemaDescriptor(BaseModel):
    source_file: str
    schemas: list[ExtractedSchema] = Field(description="Every model/entity/DTO/record class in this file. Extract ALL of them — do not skip any.")

    def to_json_schema_dict(self) -> dict[str, dict]:
        """Convert structured schemas to JSON Schema dict format for downstream consumption."""
        result = {}
        for schema in self.schemas:
            result[schema.name] = _extracted_to_json_schema(schema)
        return result


def _extracted_to_json_schema(schema: ExtractedSchema) -> dict:
    """Convert an ExtractedSchema to a JSON Schema object."""
    props: dict[str, dict] = {}
    for p in schema.properties:
        prop_schema = _property_to_json_schema(p)
        props[p.name] = prop_schema

    result: dict = {"type": "object", "properties": props}

    if schema.required_fields:
        result["required"] = schema.required_fields

    if schema.parent_ref:
        # Use allOf for inheritance
        parent_ref = f"#/components/schemas/{schema.parent_ref}"
        child = result
        result = {"allOf": [{"$ref": parent_ref}, child]}

    return result


def _property_to_json_schema(p: SchemaProperty) -> dict:
    """Convert a SchemaProperty to a JSON Schema property object."""
    if p.ref:
        base: dict = {"$ref": f"#/components/schemas/{p.ref}"}
    elif p.enum_values:
        base = {"type": "string", "enum": p.enum_values}
    else:
        base = {"type": p.type}
        if p.format:
            base["format"] = p.format

    if p.is_array:
        schema: dict = {"type": "array", "items": base}
    else:
        schema = base

    if p.nullable:
        schema["nullable"] = True

    if p.constraints:
        schema.update(p.constraints)

    return schema


# --- State Models ---


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


# --- Scout Working State ---


class ScoutWorkingState(BaseModel):
    """Re-injected at every ReAct step to prevent context loss."""

    framework: str | None = None
    language: str | None = None
    route_files: list[str] = []
    servers: list[str] = []
    base_path: str = ""
    scratchpad: str = ""
    remaining_tasks: list[str] = [
        "identify_framework",
        "find_route_files",
        "find_servers",
    ]
