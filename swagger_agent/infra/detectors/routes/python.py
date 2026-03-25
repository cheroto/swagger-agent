"""Route patterns for Python frameworks."""

from swagger_agent.infra.detectors.routes._base import RoutePattern

PATTERNS: dict[str, list[RoutePattern]] = {
    "fastapi": [
        ("**/*.py", r"@(app|router)\.(get|post|put|patch|delete|api_route)\s*\("),
        ("**/*.py", r"\.include_router\s*\("),
    ],
    "flask": [
        ("**/*.py", r"@(app|blueprint|bp|api)\.(route|get|post|put|patch|delete)\s*\("),
        ("**/*.py", r"\.add_url_rule\s*\("),
    ],
    "django": [
        ("**/urls.py", r"(path|re_path|url)\s*\("),
        ("**/*.py", r"@(api_view|action)\s*\("),
        ("**/*.py", r"\.(register)\s*\("),  # DefaultRouter/SimpleRouter.register()
    ],
    "starlette": [
        ("**/*.py", r"Route\s*\("),
    ],
    "tornado": [
        ("**/*.py", r"(url|URLSpec)\s*\("),
    ],
    "falcon": [
        ("**/*.py", r"(app|api)\.add_route\s*\("),
    ],
    "sanic": [
        ("**/*.py", r"@(app|bp)\.(get|post|put|patch|delete|route)\s*\("),
    ],
    "bottle": [
        ("**/*.py", r"@(app|route)\.(get|post|put|patch|delete|route)\s*\("),
    ],
}
