"""Route patterns for Swift frameworks."""

from swagger_agent.infra.detectors.routes._base import RoutePattern

PATTERNS: dict[str, list[RoutePattern]] = {
    "vapor": [
        ("**/*.swift", r"(RouteCollection|func\s+boot\s*\(\s*routes\s*:)"),
        ("**/*.swift", r"\.(get|post|put|patch|delete)\s*\("),
    ],
}
