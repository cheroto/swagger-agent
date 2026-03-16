"""Route Extractor agent prompts — two-phase architecture.

Phase 1 (CODE_ANALYSIS_PROMPT): Tech-agnostic observation of the route file.
Phase 2 (build_phase2_prompt): Dynamically assembled extraction prompt.
"""

from __future__ import annotations

import re

from swagger_agent.models import CodeAnalysis


# ---------------------------------------------------------------------------
# Phase 1: Code Analysis
# ---------------------------------------------------------------------------

CODE_ANALYSIS_PROMPT = """\
You are a code analyst. Read the route file and report what you observe.
Do NOT extract full endpoint details — just identify patterns and structure.
Fill every field in the response schema. All field semantics are defined in the schema descriptions.
"""


# ---------------------------------------------------------------------------
# Phase 2: Endpoint Extraction (dynamically assembled)
# ---------------------------------------------------------------------------

_PHASE2_OUTPUT_FORMAT = """\
## Strategy

- Combine base_path + router-level prefix + endpoint-level path. Convert path parameters to OpenAPI {param} syntax.
- For serverless handlers with no route decorators, infer path from directory/filename structure and HTTP method from filename prefix or code behavior.
- For route resource declarations, expand into individual CRUD endpoints.
- Every path parameter segment in the URL must have a matching Parameter object.
- Each endpoint's parameters are independent — do not share between endpoints.
- Extract ALL endpoints in the file. Do not skip any.
- source_file is set by the harness — ignore it.

All field semantics, valid values, and decision rules are defined in the schema descriptions provided by the tool definition. Follow them precisely.
"""


def build_phase2_prompt(
    analysis: CodeAnalysis,
    base_path: str,
    mount_prefix: str = "",
    default_auth_hint: str = "",
    default_auth_mode: str = "",
) -> str:
    """Build the Phase 2 extraction prompt from Phase 1 observations.

    Deterministic function — no LLM calls.
    """
    sections = ["You are the Route Extractor agent. Extract every HTTP endpoint from this route file into structured format.\n"]

    # --- Dynamic context from Phase 1 ---
    sections.append("## Code Observations\n")
    sections.append(f"- Routing style: {analysis.routing_style}")
    sections.append(f"- Path parameter syntax: {analysis.path_param_syntax}")

    # Combine base_path + mount_prefix + file-level prefix
    effective_prefix = base_path.rstrip("/")
    if mount_prefix:
        effective_prefix = effective_prefix + "/" + mount_prefix.strip("/")
    if analysis.base_prefix:
        # Only add file-level prefix if it's not already a suffix of effective_prefix by path segments
        file_prefix = analysis.base_prefix.strip("/")
        if file_prefix:
            eff_segments = effective_prefix.strip("/").split("/") if effective_prefix.strip("/") else []
            file_segments = file_prefix.split("/")
            if eff_segments[-len(file_segments):] != file_segments:
                effective_prefix = effective_prefix + "/" + file_prefix

    prefix = effective_prefix or analysis.base_prefix or base_path
    if prefix:
        sections.append(f"- Base prefix: {prefix}")

    sections.append(f"- Request bodies: {analysis.request_body_style}")
    sections.append(f"- Error handling: {analysis.error_handling_notes}")

    # --- Auth context (data only — classification rules are in Endpoint.security field description) ---
    if analysis.auth_patterns:
        sections.append("\n## Auth Patterns Observed\n")
        for ap in analysis.auth_patterns:
            scheme_name = _scheme_name_from_type(ap.scheme_type)
            scheme_type = ap.scheme_type if ap.scheme_type in ("bearer", "apikey", "basic", "oauth2", "cookie") else "bearer"
            sections.append(
                f"- `{ap.indicator}` ({ap.mechanism}), "
                f"scheme: {scheme_name}/{scheme_type}, applies_to: {ap.applies_to}"
            )
    elif default_auth_hint:
        sections.append(f"\n## Global Auth Context (default_auth_mode={default_auth_mode})\n")
        sections.append(f"```\n{default_auth_hint}\n```")
    elif analysis.auth_inference_notes:
        sections.append("\n## Auth Inference Notes\n")
        sections.append(analysis.auth_inference_notes)
    else:
        sections.append("\n## Auth: none detected")

    # --- Import lines for ref resolution ---
    if analysis.import_lines:
        sections.append("\n## Available Imports (for RefHint resolution)\n")
        sections.append("```")
        sections.extend(analysis.import_lines)
        sections.append("```")
        sections.append("Use these exact import lines as import_line in RefHints when a type matches.")

    # --- Endpoint checklist ---
    if analysis.endpoints:
        sections.append(f"\n## Endpoint Checklist ({len(analysis.endpoints)} endpoints)\n")
        sections.append("You MUST extract at least these endpoints. Each endpoint's parameters are independent — do NOT mix parameters between endpoints.\n")
        for i, ep in enumerate(analysis.endpoints, 1):
            path_params = _extract_path_params(ep.path)
            sections.append(f"  {i}. {ep.method} {ep.path} → handler: {ep.handler_name}")
            if path_params:
                sections.append(f"     REQUIRED path params ({len(path_params)}): {', '.join(path_params)}")
                sections.append(f"     → You MUST output {len(path_params)} Parameter(s) with in=\"path\" for this endpoint")
        sections.append("\nExtract any additional endpoints you find beyond this list.")

    # --- Static output format ---
    sections.append("")
    sections.append(_PHASE2_OUTPUT_FORMAT)

    return "\n".join(sections)


def _extract_path_params(path: str) -> list[str]:
    """Extract path parameter names from a URL path.

    Handles {param}, :param, and <param> syntax.
    """
    params = re.findall(r"\{(\w+)\}", path)
    params += re.findall(r":(\w+)", path)
    params += re.findall(r"<(\w+)>", path)
    return params


def _scheme_name_from_type(scheme_type: str) -> str:
    """Map scheme_type string to a security scheme name."""
    mapping = {
        "bearer": "BearerAuth",
        "apikey": "ApiKeyAuth",
        "cookie": "CookieAuth",
        "basic": "BasicAuth",
        "oauth2": "OAuth2",
    }
    return mapping.get(scheme_type.lower(), "BearerAuth")


