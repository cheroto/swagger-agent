from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# --- Ref Hints ---


class RefHint(BaseModel):
    ref_hint: str
    import_source: str | None = None
    resolution: Literal["import", "class_to_file", "unresolvable"]


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


# --- Endpoint Descriptor ---


class Parameter(BaseModel):
    name: str
    in_: Literal["path", "query", "header", "cookie"] = Field(alias="in")
    required: bool = False
    schema_: dict = Field(default_factory=dict, alias="schema")

    model_config = {"populate_by_name": True}


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


# --- Schema Descriptor ---


class SchemaDescriptor(BaseModel):
    source_file: str
    schemas: dict[str, dict]


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
