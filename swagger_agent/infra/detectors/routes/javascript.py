"""Route patterns for JavaScript/TypeScript frameworks."""

from swagger_agent.infra.detectors.routes._base import RoutePattern

PATTERNS: dict[str, list[RoutePattern]] = {
    "express": [
        ("**/*.{js,ts}", r"(router|app)\.(get|post|put|patch|delete|all)\s*\("),
    ],
    "fastify": [
        ("**/*.{js,ts}", r"(fastify|app|server)\.(get|post|put|patch|delete|route)\s*\("),
    ],
    "koa": [
        ("**/*.{js,ts}", r"router\.(get|post|put|patch|delete|all)\s*\("),
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
