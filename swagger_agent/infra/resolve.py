"""Import-to-file resolver for ref_hints.

Deterministic resolution of import statements to source file paths.
Supports Java and Python imports. This is a prototype of the Ref Resolver
infrastructure module described in CLAUDE.md.
"""

from __future__ import annotations

import re
from pathlib import Path


def resolve_java_import(import_source: str, source_roots: list[Path]) -> Path | None:
    """Resolve a Java import line to a file path.

    Examples:
        "import com.sopromadze.blogapi.model.user.User;"
        → src/main/java/com/sopromadze/blogapi/model/user/User.java

        "import com.sopromadze.blogapi.model.Album;"
        → src/main/java/com/sopromadze/blogapi/model/Album.java
    """
    # Strip "import " prefix and trailing semicolon/whitespace
    cleaned = import_source.strip()
    cleaned = re.sub(r"^import\s+(static\s+)?", "", cleaned)
    cleaned = cleaned.rstrip("; \t")

    # Convert dots to path separators
    rel_path = cleaned.replace(".", "/") + ".java"

    for root in source_roots:
        candidate = root / rel_path
        if candidate.is_file():
            return candidate

    return None


def resolve_python_import(import_source: str, project_root: Path) -> Path | None:
    """Resolve a Python import line to a file path.

    Examples:
        "from app.schemas.user import UserResponse"
        → app/schemas/user.py

        "from app.models.article import Article"
        → app/models/article.py
    """
    # Parse "from X import Y" or "import X"
    match = re.match(r"from\s+([\w.]+)\s+import", import_source.strip())
    if match:
        module_path = match.group(1)
    else:
        match = re.match(r"import\s+([\w.]+)", import_source.strip())
        if not match:
            return None
        module_path = match.group(1)

    # Convert dots to path separators
    rel_path = module_path.replace(".", "/")

    # Try as file first, then as package __init__
    for suffix in [".py", "/__init__.py"]:
        candidate = project_root / (rel_path + suffix)
        if candidate.is_file():
            return candidate

    return None


def resolve_js_import(import_source: str, source_file: Path) -> Path | None:
    """Resolve a JS/TS import to a file path (relative to the importing file).

    Examples:
        "const { User } = require('./models/user')"
        → resolved relative to source_file

        "import { User } from '../models/user'"
        → resolved relative to source_file
    """
    # Match require('./path') or from './path' or from "../path"
    match = re.search(r"""(?:require\s*\(\s*['"]|from\s+['""])(\.\.?/[^'"]+)['"]""", import_source)
    if not match:
        return None

    rel_import = match.group(1)
    base_dir = source_file.parent
    candidate_base = (base_dir / rel_import).resolve()

    # Try with common extensions
    for ext in ["", ".js", ".ts", ".mjs", "/index.js", "/index.ts"]:
        candidate = Path(str(candidate_base) + ext)
        if candidate.is_file():
            return candidate

    return None


def resolve_same_package(class_name: str, referring_file: Path) -> Path | None:
    """Resolve a class by looking in the same directory as the referring file.

    Handles Java same-package access (no import required) and Python
    same-directory modules.
    """
    parent = referring_file.parent

    # Java: ClassName.java in same directory
    candidate = parent / f"{class_name}.java"
    if candidate.is_file():
        return candidate

    # Python: class_name.py (lowercase) in same directory
    candidate = parent / f"{class_name.lower()}.py"
    if candidate.is_file():
        return candidate

    return None


def resolve_import(
    import_source: str,
    framework: str,
    project_root: Path,
    source_roots: list[Path] | None = None,
    source_file: Path | None = None,
) -> Path | None:
    """Resolve an import line to a file path based on framework."""
    if framework in ("spring", "java"):
        roots = source_roots or _find_java_source_roots(project_root)
        return resolve_java_import(import_source, roots)
    elif framework in ("fastapi", "flask", "django", "python"):
        return resolve_python_import(import_source, project_root)
    elif framework in ("express", "nestjs", "node", "typescript"):
        if source_file:
            return resolve_js_import(import_source, source_file)
    return None


def _find_java_source_roots(project_root: Path) -> list[Path]:
    """Find standard Java source roots in a project."""
    candidates = [
        project_root / "src" / "main" / "java",
        project_root / "src",
        project_root,
    ]
    return [c for c in candidates if c.is_dir()]


def scan_refs_in_schemas(schemas: dict[str, dict]) -> set[str]:
    """Recursively scan JSON Schema dicts for $ref targets, return schema names.

    Extracts "Foo" from "$ref": "#/components/schemas/Foo".
    """
    refs: set[str] = set()

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            if "$ref" in obj:
                ref_val = obj["$ref"]
                if isinstance(ref_val, str) and ref_val.startswith("#/components/schemas/"):
                    refs.add(ref_val.split("/")[-1])
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(schemas)
    return refs


def parse_imports_from_file(file_path: Path, framework: str) -> dict[str, str]:
    """Parse import lines from a source file, return {ClassName: import_line}.

    This builds the class-to-file mapping incrementally as files are discovered.
    """
    content = file_path.read_text(encoding="utf-8", errors="replace")
    imports: dict[str, str] = {}

    if framework in ("spring", "java"):
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("import ") and not line.startswith("import static"):
                # "import com.foo.bar.Baz;" → class_name = "Baz"
                cleaned = line.rstrip(";").strip()
                parts = cleaned.split(".")
                if parts:
                    class_name = parts[-1]
                    imports[class_name] = line
    elif framework in ("fastapi", "flask", "django", "python"):
        for line in content.splitlines():
            line = line.strip()
            match = re.match(r"from\s+[\w.]+\s+import\s+(.+)", line)
            if match:
                names = [n.strip() for n in match.group(1).split(",")]
                for name in names:
                    # Handle "as" aliases
                    actual = name.split(" as ")[0].strip()
                    if actual and actual[0].isupper():
                        imports[actual] = line

    return imports
