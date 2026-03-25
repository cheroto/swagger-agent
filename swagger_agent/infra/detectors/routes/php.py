"""Route patterns for PHP frameworks."""

from swagger_agent.infra.detectors.routes._base import RoutePattern

PATTERNS: dict[str, list[RoutePattern]] = {
    "laravel": [
        ("routes/*.php", r"Route::(get|post|put|patch|delete|resource|apiResource|group|controller)\s*\("),
        ("**/*.php", r"Route::(get|post|put|patch|delete|apiResource)\s*\("),
    ],
    "slim": [
        ("**/*.php", r"\$(app|group)\->(get|post|put|patch|delete)\s*\("),
    ],
    "symfony": [
        ("**/*.php", r"#\[(Route|Get|Post|Put|Patch|Delete)\s*\("),
        ("config/routes*.yaml", r"(path|resource):"),
    ],
}
