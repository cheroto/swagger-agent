"""Route patterns for C# / ASP.NET frameworks."""

from swagger_agent.infra.detectors.routes._base import RoutePattern

PATTERNS: dict[str, list[RoutePattern]] = {
    "aspnetcore": [
        # Traditional controllers: [HttpGet], [HttpGet("{id}")], [Route("api/[controller]")]
        ("**/*.cs", r"\[(Http(Get|Post|Put|Patch|Delete)|Route|ApiController)(\([^]]*\))?\]"),
        # Minimal APIs: app.MapGet("/path", handler)
        ("**/*.cs", r"\.(Map(Get|Post|Put|Patch|Delete|Group|Methods))\s*\("),
    ],
}
