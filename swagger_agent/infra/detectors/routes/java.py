"""Route patterns for Java/Kotlin frameworks."""

from swagger_agent.infra.detectors.routes._base import RoutePattern

PATTERNS: dict[str, list[RoutePattern]] = {
    "spring": [
        ("**/*.{java,kt}", r"@(GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping|RequestMapping|RestController|Controller)\b"),
        ("**/*.{java,kt}", r"RouterFunction|RouterFunctions"),  # Spring WebFlux functional routing
    ],
    "quarkus": [
        ("**/*.{java,kt}", r"@(GET|POST|PUT|PATCH|DELETE|Path)\b"),
    ],
    "micronaut": [
        ("**/*.{java,kt}", r"@(Get|Post|Put|Patch|Delete|Controller)\b"),
    ],
    "jersey": [
        ("**/*.java", r"@(GET|POST|PUT|PATCH|DELETE|Path)\b"),
    ],
    "resteasy": [
        ("**/*.java", r"@(GET|POST|PUT|PATCH|DELETE|Path)\b"),
    ],
}
