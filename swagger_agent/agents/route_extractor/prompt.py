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

## Report the following:

1. ROUTING STYLE: How are routes defined? (decorators, method calls, annotations, router chain, etc.)
   Example: "router.get(path, ...handler)", "@app.route(path)", "@GetMapping(path)"

2. PATH PARAM SYNTAX: What syntax is used for path parameters?
   Example: ":param", "{param}", "<param>", or "none observed"

3. BASE PREFIX: Is there a router-level prefix applied to all routes?
   Example: "/api/users", "/articles", or "" if none

4. AUTH PATTERNS: What authentication/authorization mechanism(s) do you see?
   For each one report:
   - mechanism: How is it applied? (middleware in handler chain, decorator, annotation, guard, etc.)
   - indicator: The exact code marker (e.g. "auth.required", "@PreAuthorize", "[Authorize]", "Depends(get_current_user)")
   - scheme_type: What type of auth? ("bearer", "apikey", "cookie", "basic", "oauth2", "unknown")
   - applies_to: Scope — "all" (controller/router-wide), "per-endpoint", or "group"
   If no auth patterns are visible, return an empty list.

5. HAS AUTH IMPORTS: Are there any auth-related imports? (true/false)

5b. AUTH INFERENCE NOTES: If no explicit auth patterns are found in this file, look for indirect signals:
   - Comments mentioning middleware groups that apply auth (e.g. "assigned the api middleware group")
   - Route grouping structures that suggest external auth (e.g. Laravel Route::group, Express router.use)
   - Naming conventions suggesting auth: endpoints like GET /user (singular, no ID = "current user") or /me typically require auth
   - Serverless: checks on authorizer/claims/token in request context
   Report what you find. If nothing, leave empty.

6. REQUEST BODY STYLE: How are request bodies consumed?
   Example: "req.body (implicit JSON)", "Pydantic model in function signature", "@RequestBody annotation", "not observed"

7. ERROR HANDLING: How are errors returned?
   Example: "next(err) middleware pattern", "raise HTTPException", "throw new HttpException", "not observed"

8. IMPORT LINES: List ALL import/require/using statements from the file, exactly as they appear.

9. ENDPOINTS: List every HTTP endpoint you can identify:
   - method: HTTP method (GET, POST, PUT, PATCH, DELETE — uppercase)
   - path: Full path including any router prefix
   - handler_name: The function or method name that handles this endpoint
"""


# ---------------------------------------------------------------------------
# Phase 2: Endpoint Extraction (dynamically assembled)
# ---------------------------------------------------------------------------

_PHASE2_OUTPUT_FORMAT = """\
## Strategy

- Combine base_path + router-level prefix + endpoint-level path. Keep the framework's path param syntax as-is.
- For serverless handlers with no route decorators, infer path from directory/filename structure and HTTP method from filename prefix or code behavior.
- For route resource declarations, expand into individual CRUD endpoints.
- Every path parameter segment in the URL must have a matching Parameter object.
- Each endpoint's parameters are independent — do not share between endpoints.
- Extract ALL endpoints in the file. Do not skip any.
- source_file is set by the harness — ignore it.

All field semantics, valid values, and decision rules are defined in the schema descriptions provided by the tool definition. Follow them precisely.
"""


def build_phase2_prompt(analysis: CodeAnalysis, base_path: str) -> str:
    """Build the Phase 2 extraction prompt from Phase 1 observations.

    Deterministic function — no LLM calls.
    """
    sections = ["You are the Route Extractor agent. Extract every HTTP endpoint from this route file into structured format.\n"]

    # --- Dynamic context from Phase 1 ---
    sections.append("## Code Observations\n")
    sections.append(f"- Routing style: {analysis.routing_style}")
    sections.append(f"- Path parameter syntax: {analysis.path_param_syntax}")

    prefix = analysis.base_prefix or base_path
    if prefix:
        sections.append(f"- Base prefix: {prefix}")

    sections.append(f"- Request bodies: {analysis.request_body_style}")
    sections.append(f"- Error handling: {analysis.error_handling_notes}")

    # --- Auth instructions ---
    if analysis.auth_patterns:
        sections.append("\n## Authentication\n")
        for ap in analysis.auth_patterns:
            scheme_name = _scheme_name_from_type(ap.scheme_type)
            if ap.applies_to == "all":
                sections.append(
                    f"All endpoints in this file use `{ap.indicator}` ({ap.mechanism}). "
                    f'Set security: ["{scheme_name}"] on every endpoint.'
                )
            elif ap.applies_to == "per-endpoint":
                sections.append(
                    f"Endpoints with `{ap.indicator}` ({ap.mechanism}) require auth → "
                    f'set security: ["{scheme_name}"]. '
                    "Endpoints WITHOUT it are public → set security: []."
                )
            else:  # group
                sections.append(
                    f"Some endpoint groups use `{ap.indicator}` ({ap.mechanism}) → "
                    f'set security: ["{scheme_name}"]. '
                    "Endpoints outside the group without auth are public → set security: []."
                )
    elif not analysis.has_auth_imports and not analysis.auth_inference_notes:
        sections.append("\n## Authentication\n")
        sections.append("No auth patterns detected. Set security: [] (public) on all endpoints.")
    elif analysis.auth_inference_notes:
        sections.append("\n## Authentication\n")
        sections.append(
            "No explicit per-endpoint auth markers were found, but there are indirect signals:\n"
            f"{analysis.auth_inference_notes}\n\n"
            "Without explicit per-endpoint markers (decorators, attributes, middleware), "
            "you cannot reliably determine which endpoints require auth. "
            "Default to security: [] (public) for all endpoints in this file. "
            "Only set security: [\"BearerAuth\"] if the handler code itself "
            "explicitly checks credentials (reads a token, validates a claim, calls an auth service)."
        )
    else:
        sections.append("\n## Authentication\n")
        sections.append(
            "Auth-related imports exist but no clear per-endpoint pattern was detected. "
            "Examine each endpoint for auth indicators. If none found, set security: []."
        )

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


