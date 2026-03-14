from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# --- Ref Hints ---


class RefHint(BaseModel):
    ref_hint: str = Field(description="Type name as it appears in code. Use the inner type only, strip collection wrappers: List<Article> → 'Article'.")
    resolution: Literal["import", "class_to_file", "unresolvable", "inferred"] = Field(description="How to resolve this type. 'import' = real type, used as a type annotation (var x: MyType, param: MyType, @RequestBody MyType) and found in an import/require/using statement — provide import_line. 'class_to_file' = real type used as a type annotation, but no explicit import (same package/namespace). 'unresolvable' = framework/language builtin (Response, IActionResult, HttpResponse, int, string). 'inferred' = NO type with this name exists as a class/struct/interface/type declaration — you are inventing a descriptive name for a function call return (e.g. dtos.CreateSomething(args) returns a map/dict, not a declared type), an anonymous/inline shape ({ email: string }), or a dynamic value with no type annotation.")
    import_line: str = Field(default="", description="The exact import/require/using statement that imports this type. Only meaningful when resolution='import'. Empty string otherwise.")
    file_namespace: str = Field(default="", description="The namespace/package/module declaration of the current file (e.g. 'namespace Conduit.Features.Articles;', 'package com.example.users;'). Helps disambiguate same-name types across packages.")


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
    scheme_type: str = Field(description="Auth type: 'bearer', 'apikey', 'cookie', 'basic', 'oauth2', 'unknown'.")
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
    schema_ref: RefHint | None = Field(default=None, description="Type reference for the response body. None if response has no body (e.g. 204, 401 with no detail).")


class Endpoint(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = Field(description="HTTP method (uppercase). Only standard HTTP methods — do not include WebSocket, gRPC, or other non-HTTP protocols.")
    path: str = Field(description="Full path including base_path, using the framework's param syntax (e.g. /api/users/:id or /api/users/{id}).")
    operation_id: str = Field(description="Derived from the handler function name (e.g. 'getUser', 'create_article').")
    tags: list[str] = Field(default_factory=list, description="Grouping tags derived from controller/router name, e.g. ['Articles'].")
    security: list[str] = Field(default_factory=list, description="Security scheme names. [] = explicitly public (no auth). ['BearerAuth'] = requires auth. Always set, never omit.")
    parameters: list[Parameter] = Field(default_factory=list)
    request_body: RequestBody | None = Field(default=None, description="Include for any endpoint that consumes a request body. For POST/PUT/PATCH with unknown body shape, provide with schema_ref resolution='unresolvable'. Set null ONLY for bodyless endpoints (GET, DELETE, state toggles).")
    request_body_reason: str = Field(default="", description="When request_body is null on POST/PUT/PATCH, explain why: 'state toggle', 'action endpoint', 'webhook callback', 'no body evidence in code'. Empty string when request_body is provided or method is GET/DELETE.")
    responses: list[Response] = Field(default_factory=list, description="All response codes including errors. Auth endpoints must have 401. Endpoints with path params should have 404.")


class EndpointDescriptor(BaseModel):
    source_file: str
    endpoints: list[Endpoint]


# --- Schema Descriptor ---


class SchemaDescriptor(BaseModel):
    source_file: str
    schemas: dict[str, dict] = Field(description="Map of schema name to JSON Schema object. Each value must have 'type', 'properties', and optionally 'required' (non-empty array of field names).")


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
