"""Path normalization and path parameter extraction for OpenAPI paths."""

from __future__ import annotations

import logging

logger = logging.getLogger("swagger_agent.assembler")


def extract_path_params(path: str) -> list[str]:
    """Extract path parameter names from an OpenAPI path template.

    Parses {param} segments using simple string scanning — no regex.
    Returns names in order of appearance, e.g. "/users/{id}/posts/{postId}" → ["id", "postId"].
    """
    params: list[str] = []
    i = 0
    while i < len(path):
        if path[i] == "{":
            close = path.find("}", i)
            if close == -1:
                break
            name = path[i + 1 : close]
            # Strip any constraint suffix: {param:constraint} → param
            colon = name.find(":")
            if colon != -1:
                name = name[:colon]
            if name:
                params.append(name)
            i = close + 1
        else:
            i += 1
    return params


def normalize_path_template(path: str) -> str:
    """Normalize path template for identity comparison.

    Replaces all {param} with {_} so that /api/{slug} and /api/{id}
    are recognized as the same OAS path (OAS 3.0 considers them identical).
    """
    result: list[str] = []
    i = 0
    while i < len(path):
        if path[i] == "{":
            close = path.find("}", i)
            if close == -1:
                result.append(path[i:])
                break
            result.append("{_}")
            i = close + 1
        else:
            result.append(path[i])
            i += 1
    return "".join(result)


def _normalize_path(base_path: str, endpoint_path: str) -> str:
    """Combine base_path and endpoint path into a clean OpenAPI path.

    Handles base_path deduplication, double-slash collapse, and
    constraint stripping inside {param:constraint} segments.
    """
    stripped_base = base_path.rstrip("/")
    stripped_ep = endpoint_path.lstrip("/")
    if stripped_base and stripped_ep.startswith(stripped_base.lstrip("/")):
        full = "/" + stripped_ep
    else:
        full = stripped_base + "/" + stripped_ep

    # Collapse double slashes (simple string replacement, no regex)
    while "//" in full:
        full = full.replace("//", "/")

    if not full.startswith("/"):
        full = "/" + full

    # Strip constraints inside braces: {param:constraint} → {param}
    # This is not a framework-specific safety net — it cleans up constraints
    # the LLM was told to strip but occasionally preserves.
    cleaned: list[str] = []
    i = 0
    while i < len(full):
        if full[i] == "{":
            close = full.find("}", i)
            if close == -1:
                cleaned.append(full[i:])
                break
            content = full[i + 1 : close]
            colon = content.find(":")
            if colon != -1:
                content = content[:colon]
                logger.info(
                    "Stripped constraint from path param: {%s...} → {%s}",
                    full[i + 1 : close], content,
                )
            # Strip optional marker: {param?} → {param}
            if content.endswith("?"):
                content = content[:-1]
            # Strip default value: {param=default} → {param}
            eq = content.find("=")
            if eq != -1:
                content = content[:eq]
            # Strip catch-all prefix: {*param} → {param}
            if content.startswith("*"):
                content = content[1:]
            cleaned.append("{" + content + "}")
            i = close + 1
        else:
            cleaned.append(full[i])
            i += 1
    full = "".join(cleaned)

    # Remove trailing slash (keep root "/")
    if len(full) > 1 and full.endswith("/"):
        full = full.rstrip("/")

    return full
