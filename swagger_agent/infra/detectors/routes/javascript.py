"""Route patterns for JavaScript/TypeScript frameworks."""

from swagger_agent.infra.detectors.routes._base import RoutePattern

PATTERNS: dict[str, list[RoutePattern]] = {
    "express": [
        # Match any variable calling HTTP methods — not just 'router' or 'app'
        ("**/*.{js,ts}", r"\w+\.(get|post|put|patch|delete|all|route)\s*\(\s*['\"\/]"),
    ],
    "fastify": [
        ("**/*.{js,ts}", r"\w+\.(get|post|put|patch|delete|route)\s*\(\s*['\"\/]"),
    ],
    "koa": [
        ("**/*.{js,ts}", r"\w+\.(get|post|put|patch|delete|all)\s*\(\s*['\"\/]"),
    ],
    "hapi": [
        ("**/*.{js,ts}", r"server\.route\s*\("),
    ],
    "hono": [
        ("**/*.{js,ts}", r"(app|router)\.(get|post|put|patch|delete|all)\s*\("),
    ],
    "nestjs": [
        ("**/*.ts", r"@(Get|Post|Put|Patch|Delete|All)\s*\("),
        ("**/*.ts", r"@Controller\s*\("),
    ],
    "restify": [
        ("**/*.{js,ts}", r"server\.(get|post|put|patch|del)\s*\("),
    ],
    "adonis": [
        ("**/*.{js,ts}", r"Route\.(get|post|put|patch|delete|resource)\s*\("),
    ],
}
