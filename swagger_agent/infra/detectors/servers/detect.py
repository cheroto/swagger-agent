"""Detect server URLs (port) and base path from config and source files."""

from __future__ import annotations

import os
import re

from swagger_agent.infra.detectors._utils import glob_files, read_file_safe

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

# Language -> glob pattern for source files
_EXT_MAP: dict[str, str] = {
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

_EXT_MAP_BP: dict[str, str] = {
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


def find_servers(
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
        content = read_file_safe(path, max_bytes=8000)
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
        glob_pat = _EXT_MAP.get(language)
        if glob_pat:
            candidates = glob_files(target_dir, glob_pat)
            entry_hints = ("main", "app", "server", "index", "program", "startup")
            entry_files = [
                f for f in candidates
                if any(h in os.path.basename(f).lower() for h in entry_hints)
            ]
            for rel_path in entry_files[:10]:
                full = os.path.join(target_dir, rel_path)
                content = read_file_safe(full, max_bytes=8000)
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
        glob_pat = _EXT_MAP_BP.get(language)
        if glob_pat:
            candidates = glob_files(target_dir, glob_pat)
            for rel_path in candidates[:30]:
                full = os.path.join(target_dir, rel_path)
                content = read_file_safe(full, max_bytes=8000)
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
