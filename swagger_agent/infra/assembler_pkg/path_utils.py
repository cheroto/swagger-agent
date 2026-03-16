"""Path normalization and parameter reconciliation for OpenAPI paths."""

from __future__ import annotations

import logging
import re

from swagger_agent.models import Endpoint, Parameter

logger = logging.getLogger("swagger_agent.assembler")


def normalize_path_template(path: str) -> str:
    """Normalize path template for identity comparison.

    Replaces all {param} with {_} so that /api/{slug} and /api/{id}
    are recognized as the same OAS path (OAS 3.0 considers them identical).
    """
    return re.sub(r"\{[^}]+\}", "{_}", path)


def _replace_outside_braces(path: str, pattern: str, repl: str) -> str:
    """Apply a regex substitution only to text outside of {...} segments."""
    result: list[str] = []
    i = 0
    while i < len(path):
        if path[i] == "{":
            close = path.find("}", i)
            if close == -1:
                result.append(re.sub(pattern, repl, path[i:]))
                break
            result.append(path[i:close + 1])
            i = close + 1
        else:
            next_open = path.find("{", i)
            if next_open == -1:
                result.append(re.sub(pattern, repl, path[i:]))
                break
            result.append(re.sub(pattern, repl, path[i:next_open]))
            i = next_open
    return "".join(result)


def _sanitize_path_template(path: str) -> str:
    """Validate and fix path template issues.

    Strips unresolved route constraints ({param:constraint} → {param})
    and removes trailing slashes.
    """
    original = path

    constraint_pattern = re.compile(r"\{(\w+):[^}]+\}")
    if constraint_pattern.search(path):
        logger.warning(
            "Path has unresolved route constraints (LLM should have resolved these): %s",
            path,
        )
        path = constraint_pattern.sub(r"{\1}", path)

    # Strip optional markers: {param?} → {param}, {param=default} → {param}
    # ASP.NET uses ? for optional and =value for defaults in path templates.
    path = re.sub(r"\{(\w+)[?][^}]*\}", r"{\1}", path)
    path = re.sub(r"\{(\w+)=[^}]*\}", r"{\1}", path)

    # Strip catch-all prefix: {*param} → {param}
    # ASP.NET and other frameworks use * prefix for catch-all route segments.
    path = re.sub(r"\{\*(\w+)\}", r"{\1}", path)

    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    if path != original:
        logger.info("Path template normalized: %s → %s", original, path)

    return path


def _normalize_path(base_path: str, endpoint_path: str) -> str:
    """Combine base_path and endpoint path, normalizing to OpenAPI format."""
    stripped_base = base_path.rstrip("/")
    stripped_ep = endpoint_path.lstrip("/")
    if stripped_base and stripped_ep.startswith(stripped_base.lstrip("/")):
        full = f"/{stripped_ep}"
    else:
        full = f"{stripped_base}/{stripped_ep}"

    full = re.sub(r"//+", "/", full)
    if not full.startswith("/"):
        full = "/" + full

    # Safety nets: convert framework-specific param syntax to OpenAPI {param}.
    # The LLM should already output {param} format, but these catch stragglers.
    prev = full
    # Express/Sinatra:  :param
    full = _replace_outside_braces(full, r":(\w+)", r"{\1}")
    # Flask typed:      <int:param>, <string:param>, <uuid:param>
    full = re.sub(r"<\w+:(\w+)>", r"{\1}", full)
    # Flask untyped:    <param>
    full = re.sub(r"<(\w+)>", r"{\1}", full)
    # Corrupted Flask:  <int{param}> (LLM merges type and brace)
    full = re.sub(r"<\w+\{(\w+)\}>", r"{\1}", full)
    if full != prev:
        logger.warning(
            "Path param safety net fired (LLM should have converted these): %s → %s",
            prev, full,
        )

    full = _sanitize_path_template(full)
    return full


def _reconcile_path_params(path_key: str, ep: Endpoint) -> None:
    """Ensure parameter objects match the names in the path template.

    Renames mismatched path parameters and removes orphans. Adds missing
    path parameters that appear in the template but have no parameter object.
    """
    path_params = set(re.findall(r"\{(\w+)\}", path_key))

    if not ep.parameters:
        if not path_params:
            return
        ep.parameters = []

    # Remove orphaned path params even if path has no template params
    if not path_params:
        orphans = [p.name for p in ep.parameters if p.in_ == "path"]
        if orphans:
            for name in orphans:
                logger.info(
                    "Removing orphaned path parameter '%s' not in path template: %s",
                    name, path_key,
                )
            ep.parameters = [p for p in ep.parameters if p.in_ != "path"]
        return

    ep_path_params = {p.name for p in ep.parameters if p.in_ == "path"}

    extra_in_ep = ep_path_params - path_params
    missing_in_ep = path_params - ep_path_params

    if extra_in_ep and missing_in_ep:
        if len(extra_in_ep) == 1 and len(missing_in_ep) == 1:
            old_name = extra_in_ep.pop()
            new_name = missing_in_ep.pop()
            for p in ep.parameters:
                if p.in_ == "path" and p.name == old_name:
                    logger.info(
                        "Reconciling path parameter: %s → %s (path template: %s)",
                        old_name, new_name, path_key,
                    )
                    p.name = new_name
                    break
        else:
            norm_params = re.findall(r"\{(\w+)\}", path_key)
            missing_ordered = [p for p in norm_params if p in missing_in_ep]
            ep_extra_ordered = [
                p.name for p in ep.parameters
                if p.in_ == "path" and p.name in extra_in_ep
            ]

            pairs = min(len(missing_ordered), len(ep_extra_ordered))
            if pairs > 0:
                rename_map = dict(zip(ep_extra_ordered[:pairs], missing_ordered[:pairs]))
                for p in ep.parameters:
                    if p.in_ == "path" and p.name in rename_map:
                        logger.info(
                            "Reconciling path parameter: %s → %s (path template: %s)",
                            p.name, rename_map[p.name], path_key,
                        )
                        p.name = rename_map[p.name]

        ep_path_params = {p.name for p in ep.parameters if p.in_ == "path"}
        extra_in_ep = ep_path_params - path_params

    if extra_in_ep:
        for orphan in extra_in_ep:
            logger.info(
                "Removing orphaned path parameter '%s' not in path template: %s",
                orphan, path_key,
            )
        ep.parameters = [
            p for p in ep.parameters
            if not (p.in_ == "path" and p.name in extra_in_ep)
        ]

    # Force required=True on all path parameters (OpenAPI 3.0 requirement)
    for p in ep.parameters:
        if p.in_ == "path" and not p.required:
            logger.info(
                "Forcing required=True on path parameter '%s' (OpenAPI 3.0 requirement): %s",
                p.name, path_key,
            )
            p.required = True

    ep_path_params_final = {p.name for p in ep.parameters if p.in_ == "path"}
    still_missing = path_params - ep_path_params_final
    if still_missing:
        for name in still_missing:
            logger.info(
                "Adding missing path parameter '%s' from path template: %s",
                name, path_key,
            )
            ep.parameters.append(
                Parameter(name=name, in_="path", required=True,
                          schema_={"type": "string"})
            )
