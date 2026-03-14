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
## Path Construction

- Combine base_path + any router-level prefix + endpoint-level path to form the full path.
- Keep path parameter syntax as-is from the framework (:param, {param}, <param>).
- For serverless handlers (Lambda, Azure Functions, Cloud Functions) with no route decorators:
  1. The parent directory name is the resource (e.g. `energy/` → `energy`).
  2. The filename (without extension) is the action, but strip any HTTP method prefix: `get-history` → `history`, `post-upload` → `upload`, `delete-item` → `item`. If no prefix matches, use the full filename.
  3. Path = `/{resource}/{action}`.
  4. Infer the HTTP method from the stripped prefix (`get-` → GET, `post-` → POST, etc.). If no prefix, infer from the code (e.g. reads query params → GET, reads body → POST).
- For route resources (e.g. Laravel `Route::resource`), expand into individual CRUD endpoints.

## Parameter Extraction

- For EVERY path parameter ({param}, :param, <param>) in the final path, you MUST include a Parameter with `in: "path"` and `required: true`. Count the path parameters in the URL — the number of path Parameter objects must equal the number of `{param}` / `:param` / `<param>` segments.
- Path parameters are defined by the URL pattern — do NOT copy query parameters from other endpoints.
- Each endpoint's parameters are independent. Do not share parameters between endpoints.

## Request Body Detection

- Include request_body when you see evidence of body consumption in the handler:
  - A parameter with a body marker (e.g. [FromBody], @RequestBody, Pydantic model type, req.body usage)
  - A parameter that is a complex/model type (not a path/query param, not CancellationToken, not primitive)
- For POST/PUT/PATCH with unknown body shape: still include request_body with schema_ref using `type_origin: "inferred"` and `resolution: "unresolvable"`. A pentester needs to know the endpoint accepts a body even if the shape is unknown.
- Set request_body: null ONLY for genuinely bodyless endpoints (GET, DELETE, state toggles like /complete, /activate).
- When request_body is null on POST/PUT/PATCH, set `request_body_reason` to explain why (e.g. "state toggle", "action endpoint", "webhook callback", "no body evidence in code").

## RefHint Rules

For every type reference (request bodies, response schemas, parameter types):

### type_origin (CRITICAL — determines whether infra attempts resolution)

- `type_origin: "declared"` — this exact type name exists as a class, struct, interface, type alias, or schema definition somewhere in the codebase, OR appears in an import statement. Infrastructure WILL attempt to find and extract it.
- `type_origin: "inferred"` — NO type with this name exists in the code. You are inventing a descriptive name for an anonymous/inline type (e.g. `{ email: string; password: string }`) or a factory function return (e.g. `map[string]interface{}`). Infrastructure will NOT attempt resolution — it will inline as `{type: "object"}`.

**Decision rule:** Can you point to a line in the code that says `class X`, `struct X`, `type X`, `interface X`, or `import X`? If yes → `declared`. If no → `inferred`.

### resolution (only meaningful when type_origin is "declared")

1. **Type appears in the import/require/using statements:**
   - `resolution: "import"`, `import_line`: the exact import statement

2. **Type is used in the code but has NO import line** — same package/namespace/module:
   - `resolution: "class_to_file"`, `import_line`: empty

3. **Framework/language built-in types ONLY:**
   - `resolution: "unresolvable"`
   - STRICTLY for: built-in types (dict, object, string, int, Any), framework base types (Response, IActionResult, HttpResponse, ActionResult, Task), and generic containers with no named inner type.

**IMPORTANT:** Always set `file_namespace` to the namespace/package declaration at the top of the current file. This is critical for disambiguation.

When emitting ref_hint names, use the **inner type only** — strip collection wrappers. For example: `List<Article>` → ref_hint: "Article", `Vec<User>` → ref_hint: "User", `Article[]` → ref_hint: "Article".

## Error Response Rules

1. Auth-protected endpoints MUST include 401 (Unauthorized). Add 403 if role-based access is visible.
2. Endpoints with typed request bodies: add 422 (Validation Error).
3. Endpoints with path parameters: add 404 (Not Found).
4. Look for explicit error handling (try/catch, error middleware, custom error classes).
5. Always include the success response: POST→201, DELETE→200/204, GET/PUT/PATCH→200.

## Content Type Detection

- File upload fields (UploadFile, multer, MultipartFile) → multipart/form-data
- Form data without files → application/x-www-form-urlencoded
- Default → application/json

## Operation ID

- Derive from handler function name (e.g. create_user, getArticle)
- Must be unique within the file
- If too generic (index, handler), prefix with HTTP method and resource

## Tags

- Derive from route file name, router prefix, or controller class name
- Use PascalCase (e.g. "Users", "Articles")

## Important

- Extract ALL endpoints. Do not skip any.
- source_file is set by the harness — ignore it.
- When in doubt between "class_to_file" and "unresolvable": if the type has a domain-specific name (not a language/framework built-in), use "class_to_file".
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


