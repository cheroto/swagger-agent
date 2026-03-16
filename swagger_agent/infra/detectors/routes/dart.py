"""Route patterns for Dart frameworks."""

from swagger_agent.infra.detectors.routes._base import RoutePattern

PATTERNS: dict[str, list[RoutePattern]] = {
    "dart_frog": [
        ("**/routes/**/*.dart", r"(Future<Response>|Response)\s+\w+\s*\("),
    ],
    "shelf": [
        ("**/*.dart", r"Router\(\)"),
    ],
}
