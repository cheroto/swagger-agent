"""Deterministic pre-scan of a target directory.

Runs before the Scout agent to produce tentative findings (framework,
language, route files, servers, base path) using heuristics and pattern
matching. No LLM calls. The output seeds the Scout's initial state so it
can confirm/refine rather than explore from scratch.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger("swagger_agent.prescan")

# Directories to skip during file walking (mirrors scout tools)
_SKIP_DIRS = frozenset((
    "node_modules", "__pycache__", ".git", "venv", ".venv", "dist", "build",
))


# ---------------------------------------------------------------------------
# File utilities (local copies to avoid circular imports with scout tools)
# ---------------------------------------------------------------------------

def _expand_braces(pattern: str) -> list[str]:
    """Expand brace expressions like *.{js,ts} into [*.js, *.ts]."""
    m = re.search(r"\{([^}]+)\}", pattern)
    if not m:
        return [pattern]
    prefix = pattern[:m.start()]
    suffix = pattern[m.end():]
    alternatives = m.group(1).split(",")
    expanded = []
    for alt in alternatives:
        expanded.extend(_expand_braces(prefix + alt.strip() + suffix))
    return expanded


def _glob_impl(target_dir: str, pattern: str) -> list[str]:
    """Find files matching a glob pattern under target_dir."""
    import fnmatch

    patterns = _expand_braces(pattern)
    matches = []
    seen: set[str] = set()
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS]
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, target_dir)
            if rel in seen:
                continue
            for p in patterns:
                if fnmatch.fnmatch(rel, p):
                    matches.append(rel)
                    seen.add(rel)
                    break
    matches.sort()
    return matches


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class PrescanResult:
    framework: str | None = None
    language: str | None = None
    route_files: list[str] = field(default_factory=list)
    servers: list[str] = field(default_factory=list)
    base_path: str = ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Framework detection
# ---------------------------------------------------------------------------

# (dependency_name_or_pattern, canonical_framework, canonical_language)
_JS_FRAMEWORKS: list[tuple[str, str, str]] = [
    ("express", "express", "javascript"),
    ("@nestjs/core", "nestjs", "typescript"),
    ("fastify", "fastify", "javascript"),
    ("koa", "koa", "javascript"),
    ("@hapi/hapi", "hapi", "javascript"),
    ("hapi", "hapi", "javascript"),
    ("hono", "hono", "javascript"),
    ("@hono/node-server", "hono", "javascript"),
    ("restify", "restify", "javascript"),
    ("@adonisjs/core", "adonis", "javascript"),
]

_PY_FRAMEWORKS: list[tuple[str, str]] = [
    ("fastapi", "fastapi"),
    ("flask", "flask"),
    ("django", "django"),
    ("starlette", "starlette"),
    ("tornado", "tornado"),
    ("falcon", "falcon"),
    ("sanic", "sanic"),
    ("bottle", "bottle"),
]

_JAVA_FRAMEWORKS: list[tuple[str, str]] = [
    ("spring-boot-starter-web", "spring"),
    ("spring-boot-starter-webflux", "spring"),
    ("spring-webmvc", "spring"),
    ("jersey-server", "jersey"),
    ("resteasy", "resteasy"),
    ("quarkus-resteasy", "quarkus"),
    ("micronaut-http-server", "micronaut"),
]

_GO_FRAMEWORKS: list[tuple[str, str]] = [
    ("github.com/gin-gonic/gin", "gin"),
    ("github.com/labstack/echo", "echo"),
    ("github.com/gofiber/fiber", "fiber"),
    ("github.com/go-chi/chi", "chi"),
    ("github.com/gorilla/mux", "gorilla"),
]

_RUBY_FRAMEWORKS: list[tuple[str, str]] = [
    ("rails", "rails"),
    ("sinatra", "sinatra"),
    ("grape", "grape"),
    ("roda", "roda"),
]

_RUST_FRAMEWORKS: list[tuple[str, str]] = [
    ("actix-web", "actix-web"),
    ("axum", "axum"),
    ("rocket", "rocket"),
    ("warp", "warp"),
]

_PHP_FRAMEWORKS: list[tuple[str, str]] = [
    ("laravel/framework", "laravel"),
    ("slim/slim", "slim"),
    ("symfony/framework-bundle", "symfony"),
    ("lumen", "lumen"),
]

_CSHARP_FRAMEWORKS: list[tuple[str, str]] = [
    ("Microsoft.AspNetCore", "aspnetcore"),
    ("Microsoft.NET.Sdk.Web", "aspnetcore"),
]


def _read_file_safe(path: str, max_bytes: int = 64_000) -> str:
    """Read a file, returning '' on any error."""
    try:
        with open(path, "r", errors="ignore") as f:
            return f.read(max_bytes)
    except OSError:
        return ""


def _detect_from_package_json(target_dir: str) -> tuple[str | None, str | None, list[str]]:
    """Detect framework from package.json."""
    path = os.path.join(target_dir, "package.json")
    if not os.path.isfile(path):
        return None, None, []

    content = _read_file_safe(path)
    if not content:
        return None, None, []

    try:
        pkg = json.loads(content)
    except json.JSONDecodeError:
        return None, None, ["Found package.json but failed to parse"]

    deps: dict = {}
    deps.update(pkg.get("dependencies", {}))
    deps.update(pkg.get("devDependencies", {}))

    for dep_name, fw, lang in _JS_FRAMEWORKS:
        if dep_name in deps:
            # Refine JS vs TS
            if lang == "javascript" and os.path.isfile(os.path.join(target_dir, "tsconfig.json")):
                lang = "typescript"
            return fw, lang, [f"Found {dep_name} in package.json dependencies"]

    return None, None, ["Found package.json but no recognized web framework"]


def _detect_from_python(target_dir: str) -> tuple[str | None, str | None, list[str]]:
    """Detect framework from Python config files."""
    candidates = [
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "setup.cfg",
        "Pipfile",
    ]

    for cfg_file in candidates:
        path = os.path.join(target_dir, cfg_file)
        if not os.path.isfile(path):
            continue

        content = _read_file_safe(path).lower()
        for dep_name, fw in _PY_FRAMEWORKS:
            # Match the package name as a word boundary (handle ==, >=, lines, etc.)
            if re.search(rf"\b{re.escape(dep_name)}\b", content):
                return fw, "python", [f"Found {dep_name} in {cfg_file}"]

    return None, None, []


def _detect_from_java(target_dir: str) -> tuple[str | None, str | None, list[str]]:
    """Detect framework from pom.xml or build.gradle."""
    for cfg_file in ("pom.xml", "build.gradle", "build.gradle.kts"):
        path = os.path.join(target_dir, cfg_file)
        if not os.path.isfile(path):
            continue

        content = _read_file_safe(path)
        lang = "kotlin" if cfg_file.endswith(".kts") else "java"

        for dep_name, fw in _JAVA_FRAMEWORKS:
            if dep_name in content:
                return fw, lang, [f"Found {dep_name} in {cfg_file}"]

    return None, None, []


def _detect_from_go(target_dir: str) -> tuple[str | None, str | None, list[str]]:
    """Detect framework from go.mod."""
    path = os.path.join(target_dir, "go.mod")
    if not os.path.isfile(path):
        return None, None, []

    content = _read_file_safe(path)
    for dep_path, fw in _GO_FRAMEWORKS:
        if dep_path in content:
            return fw, "go", [f"Found {dep_path} in go.mod"]

    if "net/http" in content or "module " in content:
        return "net/http", "go", ["Found go.mod, assuming net/http stdlib"]

    return None, None, []


def _detect_from_ruby(target_dir: str) -> tuple[str | None, str | None, list[str]]:
    """Detect framework from Gemfile."""
    path = os.path.join(target_dir, "Gemfile")
    if not os.path.isfile(path):
        return None, None, []

    content = _read_file_safe(path)
    for dep_name, fw in _RUBY_FRAMEWORKS:
        if re.search(rf"""gem\s+['"]({re.escape(dep_name)})['"]""", content):
            return fw, "ruby", [f"Found {dep_name} in Gemfile"]

    return None, None, []


def _detect_from_rust(target_dir: str) -> tuple[str | None, str | None, list[str]]:
    """Detect framework from Cargo.toml."""
    path = os.path.join(target_dir, "Cargo.toml")
    if not os.path.isfile(path):
        return None, None, []

    content = _read_file_safe(path)
    for dep_name, fw in _RUST_FRAMEWORKS:
        if dep_name in content:
            return fw, "rust", [f"Found {dep_name} in Cargo.toml"]

    return None, None, []


def _detect_from_php(target_dir: str) -> tuple[str | None, str | None, list[str]]:
    """Detect framework from composer.json."""
    path = os.path.join(target_dir, "composer.json")
    if not os.path.isfile(path):
        return None, None, []

    content = _read_file_safe(path)
    try:
        pkg = json.loads(content)
    except json.JSONDecodeError:
        return None, None, ["Found composer.json but failed to parse"]

    require = pkg.get("require", {})
    for dep_name, fw in _PHP_FRAMEWORKS:
        if dep_name in require:
            return fw, "php", [f"Found {dep_name} in composer.json"]

    return None, None, []


def _detect_from_csharp(target_dir: str) -> tuple[str | None, str | None, list[str]]:
    """Detect ASP.NET from .csproj files or .sln presence."""
    # Also check for .sln files as a hint this is a .NET project
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS
                   and d not in ("bin", "obj")]
        for fname in files:
            if fname.endswith(".csproj"):
                content = _read_file_safe(os.path.join(root, fname))
                for dep_name, fw in _CSHARP_FRAMEWORKS:
                    if dep_name in content:
                        rel = os.path.relpath(os.path.join(root, fname), target_dir)
                        return fw, "csharp", [f"Found {dep_name} in {rel}"]
    return None, None, []


def _detect_framework(target_dir: str) -> tuple[str | None, str | None, list[str]]:
    """Detect framework and language from config files.

    Returns (framework, language, notes).
    """
    detectors = [
        _detect_from_package_json,
        _detect_from_python,
        _detect_from_java,
        _detect_from_go,
        _detect_from_ruby,
        _detect_from_rust,
        _detect_from_php,
        _detect_from_csharp,
    ]

    all_notes: list[str] = []
    for detector in detectors:
        fw, lang, notes = detector(target_dir)
        all_notes.extend(notes)
        if fw is not None:
            return fw, lang, all_notes

    return None, None, all_notes or ["No recognized config files found"]


# ---------------------------------------------------------------------------
# Route file detection
# ---------------------------------------------------------------------------

# Maps framework -> list of (glob_pattern, grep_regex)
_ROUTE_PATTERNS: dict[str, list[tuple[str, str]]] = {
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
    "spring": [
        ("**/*.{java,kt}", r"@(GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping|RequestMapping)\b"),
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
    "laravel": [
        ("routes/*.php", r"Route::(get|post|put|patch|delete|resource|apiResource|group)\s*\("),
        ("**/*.php", r"Route::(get|post|put|patch|delete)\s*\("),
    ],
    "slim": [
        ("**/*.php", r"\$(app|group)\->(get|post|put|patch|delete)\s*\("),
    ],
    "symfony": [
        ("**/*.php", r"#\[(Route|Get|Post|Put|Patch|Delete)\s*\("),
        ("config/routes*.yaml", r"(path|resource):"),
    ],
    "aspnetcore": [
        ("**/*.cs", r"\[(Http(Get|Post|Put|Patch|Delete)|Route|ApiController)\]"),
    ],
}

# File patterns that should be excluded from route detection
_ROUTE_FILE_EXCLUDES = re.compile(
    r"(^|/)(test[s_]?/|__test__|spec/|\.test\.|\.spec\.|test_|_test\.|"
    r"migrations?/|db/migrate/|seeds?/|fixtures?/|"
    r"node_modules/|vendor/|dist/|build/|"
    r"conftest\.py$|setup\.py$|manage\.py$|wsgi\.py$|asgi\.py$|"
    r"webpack|jest|babel|eslint|prettier|tsconfig|"
    r"\.d\.ts$|\.min\.)"
)


def _grep_files_matching(target_dir: str, glob_pattern: str, grep_pattern: str) -> list[str]:
    """Find files matching glob_pattern that contain grep_pattern.

    Returns deduplicated list of relative file paths. No match cap.
    """
    files = _glob_impl(target_dir, glob_pattern)
    try:
        compiled = re.compile(grep_pattern)
    except re.error:
        return []

    matching: list[str] = []
    seen: set[str] = set()
    for rel_path in files:
        if rel_path in seen:
            continue
        if _ROUTE_FILE_EXCLUDES.search(rel_path):
            continue
        full = os.path.join(target_dir, rel_path)
        try:
            with open(full, "r", errors="ignore") as f:
                for line in f:
                    if compiled.search(line):
                        matching.append(rel_path)
                        seen.add(rel_path)
                        break
        except OSError:
            continue

    return matching


def _find_route_files(
    target_dir: str,
    framework: str | None,
    language: str | None,
) -> tuple[list[str], list[str]]:
    """Find tentative route files based on framework-specific patterns.

    Returns (route_files, notes).
    """
    if framework is None:
        return [], ["No framework detected, skipping route file detection"]

    patterns = _ROUTE_PATTERNS.get(framework)
    if patterns is None:
        return [], [f"No route patterns defined for framework '{framework}'"]

    all_files: list[str] = []
    seen: set[str] = set()
    notes: list[str] = []

    for glob_pat, grep_pat in patterns:
        matches = _grep_files_matching(target_dir, glob_pat, grep_pat)
        for f in matches:
            if f not in seen:
                all_files.append(f)
                seen.add(f)

    if all_files:
        notes.append(
            f"Found {len(all_files)} tentative route file(s) "
            f"matching {framework} patterns"
        )
    else:
        notes.append(
            f"No files matched {framework} route patterns - "
            f"Scout should verify manually"
        )

    return all_files, notes


# ---------------------------------------------------------------------------
# Server / base path detection
# ---------------------------------------------------------------------------

# Patterns to find port numbers in source/config
_PORT_PATTERNS = [
    # .env / .env.example
    (r"^PORT\s*=\s*(\d+)", "env"),
    # JS: .listen(3000) or .listen(PORT)
    (r"\.listen\s*\(\s*(\d{2,5})", "source"),
    # Python: uvicorn.run(..., port=8000)
    (r"port\s*=\s*(\d{2,5})", "source"),
    # Java: server.port=8080
    (r"server\.port\s*=\s*(\d{2,5})", "config"),
    # Generic: PORT = 3000 / const PORT = 3000
    (r"\bPORT\s*=\s*(\d{2,5})", "source"),
]

# Patterns to find base path / API prefix
_BASE_PATH_PATTERNS = [
    # Express: app.use('/api', ...) — skip /docs, /static, /public, /health
    (r"app\.use\s*\(\s*['\"](/(?!docs|static|public|health|swagger)[a-zA-Z0-9/_-]+)['\"]", "source"),
    # FastAPI: app = FastAPI(root_path=...)
    (r"root_path\s*=\s*['\"](/[a-zA-Z0-9/_-]+)['\"]", "source"),
    # FastAPI/Flask: prefix='...'
    (r"prefix\s*=\s*['\"](/[a-zA-Z0-9/_-]+)['\"]", "source"),
    # Spring: server.servlet.context-path=/api
    (r"context-path\s*=\s*(/[a-zA-Z0-9/_-]+)", "config"),
    # Generic
    (r"(?:BASE_PATH|API_PREFIX|BASE_URL)\s*=\s*['\"]?(/[a-zA-Z0-9/_-]+)", "config"),
]

# Default ports by language
_DEFAULT_PORTS: dict[str, int] = {
    "javascript": 3000,
    "typescript": 3000,
    "python": 8000,
    "java": 8080,
    "kotlin": 8080,
    "go": 8080,
    "ruby": 3000,
    "rust": 8080,
    "php": 8000,
    "csharp": 5000,
}


def _find_servers(
    target_dir: str,
    framework: str | None,
    language: str | None,
) -> tuple[list[str], str, list[str]]:
    """Detect server URLs and base path.

    Returns (servers, base_path, notes).
    """
    notes: list[str] = []
    port: int | None = None
    base_path = ""

    # Check config files first
    config_files = [
        ".env", ".env.example", ".env.local",
        "application.properties", "application.yml", "application.yaml",
    ]
    for cfg in config_files:
        path = os.path.join(target_dir, cfg)
        if not os.path.isfile(path):
            continue
        content = _read_file_safe(path, max_bytes=8000)
        for pattern, _source in _PORT_PATTERNS:
            m = re.search(pattern, content, re.MULTILINE)
            if m:
                try:
                    port = int(m.group(1))
                    notes.append(f"Found port {port} in {cfg}")
                    break
                except (ValueError, IndexError):
                    pass
        if port:
            break

    # Check source files for port if not found in config
    if port is None and language:
        ext_map = {
            "javascript": "**/*.{js,ts}",
            "typescript": "**/*.{ts,js}",
            "python": "**/*.py",
            "java": "**/*.java",
            "kotlin": "**/*.kt",
            "go": "**/*.go",
            "ruby": "**/*.rb",
            "rust": "**/*.rs",
            "php": "**/*.php",
            "csharp": "**/*.cs",
        }
        glob_pat = ext_map.get(language)
        if glob_pat:
            # Only check entry-point-like files
            candidates = _glob_impl(target_dir, glob_pat)
            entry_hints = ("main", "app", "server", "index", "program", "startup")
            entry_files = [
                f for f in candidates
                if any(h in os.path.basename(f).lower() for h in entry_hints)
            ]
            for rel_path in entry_files[:10]:  # Cap to avoid scanning too many
                full = os.path.join(target_dir, rel_path)
                content = _read_file_safe(full, max_bytes=8000)
                for pattern, _source in _PORT_PATTERNS:
                    m = re.search(pattern, content)
                    if m:
                        try:
                            port = int(m.group(1))
                            notes.append(f"Found port {port} in {rel_path}")
                            break
                        except (ValueError, IndexError):
                            pass
                if port:
                    break

    # Check for base path in route files and entry points
    if language:
        ext_map_bp = {
            "javascript": "**/*.{js,ts}",
            "typescript": "**/*.{ts,js}",
            "python": "**/*.py",
            "java": "**/*.{java,properties,yml,yaml}",
            "kotlin": "**/*.{kt,properties,yml,yaml}",
            "go": "**/*.go",
            "ruby": "**/*.rb",
            "rust": "**/*.rs",
            "php": "**/*.php",
            "csharp": "**/*.cs",
        }
        glob_pat = ext_map_bp.get(language)
        if glob_pat:
            candidates = _glob_impl(target_dir, glob_pat)
            # Prioritize entry files and config
            for rel_path in candidates[:30]:
                full = os.path.join(target_dir, rel_path)
                content = _read_file_safe(full, max_bytes=8000)
                for pattern, _source in _BASE_PATH_PATTERNS:
                    m = re.search(pattern, content)
                    if m:
                        base_path = m.group(1)
                        notes.append(f"Found base path '{base_path}' in {rel_path}")
                        break
                if base_path:
                    break

    # Build server URL
    if port is None and language:
        port = _DEFAULT_PORTS.get(language, 3000)
        notes.append(f"Using default port {port} for {language}")

    servers = []
    if port:
        servers.append(f"http://localhost:{port}")

    return servers, base_path, notes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_prescan(target_dir: str) -> PrescanResult:
    """Run a deterministic pre-scan of the target directory.

    Returns a PrescanResult with tentative findings. All results are
    heuristic-based and should be validated by the Scout agent.
    """
    target_dir = os.path.normpath(os.path.abspath(target_dir))
    result = PrescanResult()

    # 1. Detect framework and language
    fw, lang, fw_notes = _detect_framework(target_dir)
    result.framework = fw
    result.language = lang
    result.notes.extend(fw_notes)

    # 2. Find route files
    routes, route_notes = _find_route_files(target_dir, fw, lang)
    result.route_files = routes
    result.notes.extend(route_notes)

    # 3. Find servers and base path
    servers, base_path, server_notes = _find_servers(target_dir, fw, lang)
    result.servers = servers
    result.base_path = base_path
    result.notes.extend(server_notes)

    logger.info(
        "Prescan complete: framework=%s, language=%s, %d route(s), %d server(s)",
        fw, lang, len(routes), len(servers),
    )

    return result


def prescan_to_scratchpad(result: PrescanResult) -> str:
    """Convert prescan results into an initial scratchpad for the Scout.

    The scratchpad tells the LLM what was already detected deterministically
    so it can focus on confirmation rather than exploration.
    """
    lines = [
        "## Pre-scan findings (deterministic, needs validation)",
        "",
    ]

    if result.framework:
        lines.append(f"- **Framework**: {result.framework} ({result.language})")
    else:
        lines.append("- **Framework**: not detected - needs manual identification")

    if result.route_files:
        lines.append(f"- **Route files** ({len(result.route_files)} tentative):")
        for rf in result.route_files:
            lines.append(f"  - {rf}")
    else:
        lines.append("- **Route files**: none detected - needs manual search")

    if result.servers:
        lines.append(f"- **Servers**: {', '.join(result.servers)}")
    if result.base_path:
        lines.append(f"- **Base path**: {result.base_path}")

    lines.append("")
    lines.append("### Detection notes")
    for note in result.notes:
        lines.append(f"- {note}")

    lines.append("")
    lines.append(
        "**Action**: Verify these findings. Read a few route files to confirm "
        "they contain endpoint definitions. Check if any route files were missed. "
        "Confirm the framework detection is correct. Mark tasks complete as you verify."
    )

    return "\n".join(lines)
