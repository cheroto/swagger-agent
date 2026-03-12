"""Ref resolver — three-tier language-agnostic type→file mapping.

Resolution order:
  1. Import-path file lookup — parse import_source, find file on disk directly.
  2. ctags index — universal-ctags for class/struct/interface definitions.
  3. grep fallback — pattern match for edge cases (TypedDict, Mongoose, aliases).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CtagsEntry:
    name: str
    path: Path
    line: int
    kind: str  # "class", "interface", "struct", "enum", etc.


# Kinds that represent type definitions worth resolving
_RELEVANT_KINDS = frozenset({
    "class", "interface", "struct", "enum", "type", "alias",
    "record", "trait",
})

# Directories to exclude from ctags and grep
_EXCLUDE_DIRS = [
    "node_modules", "venv", ".venv", "__pycache__", ".git",
    "dist", "build", "target", "vendor",
]

_EXCLUDE_PATTERNS = ["*.min.js", "*.lock"]

# Common source extensions for file-path resolution (order = priority for ties)
_SOURCE_EXTENSIONS = [
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".cs",
    ".go", ".rs", ".php", ".rb", ".kt", ".scala", ".swift",
]

# Patterns that indicate a line is an import/require, not a definition
_IMPORT_LINE_RE = re.compile(
    r"\b(?:require\s*\(|import\s+|from\s+\S+\s+import)\b"
)


def _find_ctags_binary() -> str:
    """Find a universal-ctags binary, raising RuntimeError if not found."""
    for candidate in [shutil.which("ctags"), "/opt/homebrew/bin/ctags"]:
        if candidate and Path(candidate).is_file():
            # Verify it's universal-ctags
            try:
                result = subprocess.run(
                    [candidate, "--version"],
                    capture_output=True, text=True, timeout=5,
                )
                if "Universal Ctags" in result.stdout:
                    return candidate
            except (subprocess.TimeoutExpired, OSError):
                continue
    raise RuntimeError(
        "universal-ctags not found. Install it:\n"
        "  macOS:  brew install universal-ctags\n"
        "  Ubuntu: sudo apt install universal-ctags\n"
        "  Arch:   sudo pacman -S ctags"
    )


def build_ctags_index(project_root: Path) -> dict[str, list[CtagsEntry]]:
    """Build a {TypeName → [CtagsEntry]} index for a project.

    Runs universal-ctags once over the project, filters to type-definition
    kinds, and returns the grouped index.
    """
    ctags_bin = _find_ctags_binary()

    cmd = [
        ctags_bin, "--output-format=json", "--fields=+n", "-R",
    ]
    for d in _EXCLUDE_DIRS:
        cmd.append(f"--exclude={d}")
    for p in _EXCLUDE_PATTERNS:
        cmd.append(f"--exclude={p}")
    cmd.append(str(project_root))

    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30,
        cwd=str(project_root),
    )

    index: dict[str, list[CtagsEntry]] = defaultdict(list)
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            tag = json.loads(line)
        except json.JSONDecodeError:
            continue

        kind = tag.get("kind", "")
        if kind not in _RELEVANT_KINDS:
            continue

        name = tag.get("name", "")
        path_str = tag.get("path", "")
        line_no = tag.get("line", 0)

        if not name or not path_str:
            continue

        # ctags paths are relative to cwd (project_root)
        abs_path = (project_root / path_str).resolve()
        index[name].append(CtagsEntry(
            name=name, path=abs_path, line=line_no, kind=kind,
        ))

    return dict(index)


def _extract_path_fragment(import_source: str) -> str | None:
    """Extract a path-like fragment from an import statement for disambiguation.

    Handles:
      - Python: "from a.b.c import X" → "a/b/c"
      - Java:   "import a.b.c.X;"     → "a/b/c/X"
      - JS/TS:  "import { X } from './path/to/mod'" → "path/to/mod"
      - JS/TS:  "const X = require('./path/to/mod')" → "path/to/mod"
    """
    # Python-style: from a.b.c import X
    m = re.match(r"from\s+([\w.]+)\s+import", import_source)
    if m:
        return m.group(1).replace(".", "/")

    # JS/TS: from './path' or require('./path')
    m = re.search(r"""(?:from\s+['"]|require\s*\(\s*['"])([^'"]+)['"]""", import_source)
    if m:
        frag = m.group(1)
        # Strip all leading ./ and ../ sequences
        frag = re.sub(r"^(?:\.\./|\./)+", "", frag)
        return frag

    # Java: import a.b.c.X;
    m = re.match(r"import\s+(static\s+)?([\w.]+)\s*;?", import_source)
    if m:
        return m.group(2).replace(".", "/")

    return None


def resolve_from_import_path(
    import_source: str | None,
    project_root: Path,
) -> Path | None:
    """Resolve a type to a file by matching the import path against the filesystem.

    Extracts a path fragment from the import statement and searches for a
    matching file on disk. This is deterministic and language-agnostic.
    """
    if not import_source:
        return None

    fragment = _extract_path_fragment(import_source)
    if not fragment:
        return None

    # Try direct path match with common extensions
    for ext in _SOURCE_EXTENSIONS:
        candidate = project_root / (fragment + ext)
        if candidate.is_file():
            return candidate.resolve()

    # Fragment may be missing a leading directory (e.g. "app/models" when the
    # actual path is "src/app/models.py"). Glob for it.
    pattern = f"**/{fragment}"
    for ext in _SOURCE_EXTENSIONS:
        matches = list(project_root.glob(pattern + ext))
        if len(matches) == 1:
            return matches[0].resolve()
        if len(matches) > 1:
            # Multiple matches — prefer shortest path (most direct match)
            return min(matches, key=lambda p: len(p.parts)).resolve()

    return None


def resolve_from_ctags(
    name: str,
    import_source: str | None,
    ctags_index: dict[str, list[CtagsEntry]],
) -> Path | None:
    """Resolve a type name to a file path using the ctags index.

    If multiple entries exist for the same name, uses import_source to
    disambiguate by matching path fragments.
    """
    entries = ctags_index.get(name)
    if not entries:
        return None

    if len(entries) == 1:
        return entries[0].path

    # Multiple entries — disambiguate with import_source
    if import_source:
        fragment = _extract_path_fragment(import_source)
        if fragment:
            # Score: does the entry path contain the fragment?
            for entry in entries:
                path_str = str(entry.path)
                if fragment in path_str:
                    return entry.path

    # No disambiguation possible — return first entry
    return entries[0].path


def resolve_by_grep(
    name: str,
    project_root: Path,
    import_source: str | None = None,
) -> Path | None:
    """Fallback resolver using grep for ctags misses.

    Catches TypedDict function-call style, Mongoose schemas, type aliases
    defined via assignment, etc. Filters out import/require lines and uses
    import_source path fragment for disambiguation when multiple matches exist.
    """
    pattern = rf"(class|interface|type|struct|enum)\s+{re.escape(name)}\b|{re.escape(name)}\s*="

    include_args = []
    for ext in ["*.py", "*.ts", "*.js", "*.java", "*.cs", "*.go", "*.rs", "*.php", "*.rb"]:
        include_args.extend(["--include", ext])

    exclude_args = []
    for d in _EXCLUDE_DIRS:
        exclude_args.append(f"--exclude-dir={d}")

    # Use -n (line numbers) to inspect matching lines, not just file names
    cmd = [
        "grep", "-rn", "-E", pattern,
        *include_args, *exclude_args,
        str(project_root),
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    # Collect files that have real definitions (not import/require lines)
    candidate_files: list[Path] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: /path/to/file.js:42:matching line content
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file_path = parts[0]
        match_content = parts[2]

        # Skip lines that are imports/requires, not definitions
        if _IMPORT_LINE_RE.search(match_content):
            continue

        if file_path not in seen:
            seen.add(file_path)
            candidate_files.append(Path(file_path).resolve())

    if not candidate_files:
        return None

    if len(candidate_files) == 1:
        return candidate_files[0]

    # Disambiguate with import_source path fragment
    if import_source:
        fragment = _extract_path_fragment(import_source)
        if fragment:
            for f in candidate_files:
                if fragment in str(f):
                    return f

    return candidate_files[0]


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
