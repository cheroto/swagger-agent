"""Route patterns for Go frameworks."""

from swagger_agent.infra.detectors.routes._base import RoutePattern

PATTERNS: dict[str, list[RoutePattern]] = {
    "gin": [
        ("**/*.go", r"\.(GET|POST|PUT|PATCH|DELETE|Handle|Any|Group)\s*\("),
    ],
    "echo": [
        ("**/*.go", r"\.(GET|POST|PUT|PATCH|DELETE|Add|Group)\s*\("),
    ],
    "fiber": [
        ("**/*.go", r"\.(Get|Post|Put|Patch|Delete|All|Group)\s*\("),
    ],
    "chi": [
        ("**/*.go", r"\.(Get|Post|Put|Patch|Delete|Route|Mount|Group)\s*\("),
    ],
    "gorilla": [
        ("**/*.go", r"\.(HandleFunc|Handle|Methods)\s*\("),
    ],
    "go-net-http": [
        ("**/*.go", r"http\.(HandleFunc|Handle|ListenAndServe)"),
    ],
    "net/http": [
        ("**/*.go", r"http\.(HandleFunc|Handle|ListenAndServe)"),
    ],
}
