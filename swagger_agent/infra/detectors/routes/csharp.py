"""Route patterns for C# / ASP.NET frameworks."""

from swagger_agent.infra.detectors.routes._base import RoutePattern

PATTERNS: dict[str, list[RoutePattern]] = {
    "aspnetcore": [
        ("**/*.cs", r"\[(Http(Get|Post|Put|Patch|Delete)|Route|ApiController)\]"),
    ],
}
