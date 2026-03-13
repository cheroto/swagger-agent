"""Route patterns for Rust frameworks."""

from swagger_agent.infra.detectors.routes._base import RoutePattern

PATTERNS: dict[str, list[RoutePattern]] = {
    "actix-web": [
        ("**/*.rs", r"#\[(get|post|put|patch|delete)\s*\("),
        ("**/*.rs", r"\.(route|resource)\s*\("),
    ],
    "axum": [
        ("**/*.rs", r"\.(get|post|put|patch|delete|route)\s*\("),
        ("**/*.rs", r"Router::new\s*\(\s*\)"),
    ],
    "rocket": [
        ("**/*.rs", r"#\[(get|post|put|patch|delete)\s*\("),
    ],
    "warp": [
        ("**/*.rs", r"warp::(get|post|put|patch|delete|path)\s*\("),
    ],
}
