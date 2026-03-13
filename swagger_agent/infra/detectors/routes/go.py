"""Route patterns for Go frameworks."""

from swagger_agent.infra.detectors.routes._base import RoutePattern

PATTERNS: dict[str, list[RoutePattern]] = {
    "gin": [
        ("**/*.go", r"\.(GET|POST|PUT|PATCH|DELETE|Handle|Any)\s*\("),
    ],
    "echo": [
        ("**/*.go", r"\.(GET|POST|PUT|PATCH|DELETE|Add)\s*\("),
    ],
    "fiber": [
        ("**/*.go", r"\.(Get|Post|Put|Patch|Delete|All)\s*\("),
    ],
    "chi": [
        ("**/*.go", r"\.(Get|Post|Put|Patch|Delete|Route|Mount)\s*\("),
    ],
    "gorilla": [
        ("**/*.go", r"\.(HandleFunc|Handle|Methods)\s*\("),
    ],
    "net/http": [
        ("**/*.go", r"http\.(HandleFunc|Handle|ListenAndServe)"),
    ],
}
