"""Route patterns for Ruby frameworks."""

from swagger_agent.infra.detectors.routes._base import RoutePattern

PATTERNS: dict[str, list[RoutePattern]] = {
    "rails": [
        ("config/routes.rb", r"(get|post|put|patch|delete|resources|resource|mount)\s"),
        ("app/controllers/**/*.rb", r"def\s+(index|show|create|update|destroy|new|edit)"),
    ],
    "sinatra": [
        ("**/*.rb", r"(get|post|put|patch|delete)\s+['\"/]"),
    ],
    "grape": [
        ("**/*.rb", r"(get|post|put|patch|delete|route)\s+['\"/:]"),
    ],
}
