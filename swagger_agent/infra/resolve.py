"""Ref resolver — language-agnostic type→file mapping via ctags + grep.

Resolution order:
  1. ctags index (primary) — universal-ctags indexes class/struct/interface/record
     definitions across all languages. import_source is used only for
     disambiguation when multiple matches exist for the same type name.
  2. grep fallback — pattern match for edge cases ctags misses (TypedDict,
     Mongoose schemas, type aliases defined via assignment, etc.).

import_source is NEVER used as a standalone file resolver. It is only a
disambiguation signal passed into tiers 1 and 2.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from swagger_agent.infra.ctags_patterns import CUSTOM_CTAGS_PATTERNS


@dataclass
class CtagsEntry:
    name: str
    path: Path
    line: int
    kind: str  # "class", "interface", "struct", "enum", etc.
    scope: str | None = None  # parent scope from ctags (e.g. "Create", "Conduit.Features.Articles.Create")


# Kinds that represent type definitions worth resolving
_RELEVANT_KINDS = frozenset({
    "class", "interface", "struct", "enum", "type", "alias",
    "record", "trait", "model",
})

# Directories to exclude from ctags and grep
_EXCLUDE_DIRS = [
    "node_modules", "venv", ".venv", "__pycache__", ".git",
    "dist", "build", "target", "vendor",
]

_EXCLUDE_PATTERNS = ["*.min.js", "*.lock"]

# Common source extensions (order = priority for ties)
_SOURCE_EXTENSIONS = [
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".cs",
    ".go", ".rs", ".php", ".rb", ".kt", ".scala", ".swift",
]

# Language-agnostic pattern matching import/use/require lines (not definitions).
# Used by grep fallback to filter out false positives.
_IMPORT_LINE_RE = re.compile(
    r"\b(?:"
    r"require\s*\("              # JS:     require(...)
    r"|require_relative\s+"      # Ruby:   require_relative '...'
    r"|import\s+"                # Python/Java/Go/TS/Kotlin: import ...
    r"|from\s+\S+\s+import"      # Python: from x import y
    r"|using\s+[\w.]+"           # C#:     using Namespace.Name
    r"|use\s+[\w\\\\:]+"         # Rust:   use crate::mod  /  PHP: use App\Model
    r"|include\s+"               # Ruby/C: include ...
    r")\b"
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
    # Add custom kind definitions and regex patterns for framework-specific
    # model registrations (mongoose.model, sequelize.define, etc.)
    for kinddef, regex in CUSTOM_CTAGS_PATTERNS:
        if kinddef is not None:
            cmd.append(kinddef)
        cmd.append(regex)
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
            scope=tag.get("scope"),
        ))

    return dict(index)


def _extract_path_fragment(import_source: str) -> str | None:
    """Extract a path-like fragment from an import statement.

    Used ONLY to disambiguate when ctags or grep return multiple matches
    for the same type name. NOT used as a standalone file resolver.

    Handles all common import syntaxes across languages:
      - Python: "from a.b.c import X"             → "a/b/c"
      - Java:   "import a.b.c.X;"                 → "a/b/c/X"
      - JS/TS:  "import { X } from './path/mod'"   → "path/mod"
      - JS/TS:  "const X = require('./path/mod')"  → "path/mod"
      - C#:     "using A.B.C;"                     → "A/B/C"
      - PHP:    "use App\\Models\\User;"            → "App/Models/User"
      - Rust:   "use crate::models::user;"         → "models/user"
      - Go:     'import "github.com/u/pkg"'        → "github.com/u/pkg"
      - Ruby:   "require 'path/to/file'"           → "path/to/file"
    """
    # Python-style: from a.b.c import X
    m = re.match(r"from\s+([\w.]+)\s+import", import_source)
    if m:
        return m.group(1).replace(".", "/")

    # JS/TS/Ruby: from './path', require('./path'), require('path'),
    # require_relative 'path'
    m = re.search(
        r"""(?:from\s+['"]|require(?:_relative)?\s*\(?\s*['"])([^'"]+)['"]""",
        import_source,
    )
    if m:
        frag = m.group(1)
        # Strip all leading ./ and ../ sequences
        frag = re.sub(r"^(?:\.\./|\./)+", "", frag)
        return frag

    # C#: using A.B.C;
    m = re.match(r"using\s+([\w.]+)\s*;?", import_source)
    if m:
        return m.group(1).replace(".", "/")

    # PHP: use App\Models\User;
    m = re.match(r"use\s+([\w\\\\]+)\s*;?", import_source)
    if m:
        return m.group(1).replace("\\", "/")

    # Rust: use crate::models::user;  or  use crate::models::{User, Post};
    m = re.match(r"use\s+([\w:]+)(?:::\{.*\})?\s*;?", import_source)
    if m:
        frag = m.group(1)
        frag = re.sub(r"^crate::", "", frag)
        return frag.replace("::", "/")

    # Go: import "github.com/user/pkg"
    m = re.search(r'import\s+"([^"]+)"', import_source)
    if m:
        return m.group(1)

    # Java/Kotlin: import a.b.c.X;  (must come after C#/Rust/PHP checks
    # since those also use keywords that overlap)
    m = re.match(r"import\s+(static\s+)?([\w.]+)\s*;?", import_source)
    if m:
        return m.group(2).replace(".", "/")

    return None


def resolve_from_ctags(
    name: str,
    import_source: str | None,
    ctags_index: dict[str, list[CtagsEntry]],
) -> Path | None:
    """Resolve a type name to a file path using the ctags index (primary resolver).

    If multiple entries exist for the same name, uses import_source to
    disambiguate by matching path fragments. Falls back to heuristic
    scoring: files whose stem matches the type name are more likely to
    be the definition than files that merely reference the type.
    """
    # Handle dotted names (e.g. "Create.Command" → leaf="Command", parent="Create")
    if "." in name:
        parent, leaf = name.rsplit(".", 1)
        leaf_entries = ctags_index.get(leaf, [])
        # Filter to entries whose scope ends with the parent class name
        scoped = [e for e in leaf_entries if e.scope and e.scope.endswith(parent)]
        if len(scoped) == 1:
            return scoped[0].path
        if len(scoped) > 1:
            # Disambiguate with import_source
            if import_source:
                fragment = _extract_path_fragment(import_source)
                if fragment:
                    for entry in scoped:
                        if fragment in str(entry.path):
                            return entry.path
            return scoped[0].path
        # Fall through to try the full dotted name as-is

    entries = ctags_index.get(name)
    if not entries:
        return None

    if len(entries) == 1:
        return entries[0].path

    # Multiple entries — disambiguate with import_source path fragment
    if import_source:
        fragment = _extract_path_fragment(import_source)
        if fragment:
            for entry in entries:
                path_str = str(entry.path)
                if fragment in path_str:
                    return entry.path

    # Heuristic scoring when no import_source or it didn't help.
    # Prefer files whose stem matches the type name (case-insensitive),
    # e.g. "User" → User.js over UserController.js.
    name_lower = name.lower()
    best = entries[0]
    best_score = -1
    for entry in entries:
        score = 0
        stem = entry.path.stem.lower()
        if stem == name_lower:
            score += 2  # Exact stem match (User.js for "User")
        elif name_lower in stem:
            score += 1  # Partial match (user_model.js for "User")
        if score > best_score:
            best_score = score
            best = entry

    return best.path


def resolve_by_grep(
    name: str,
    project_root: Path,
    import_source: str | None = None,
) -> Path | None:
    """Fallback resolver using grep for ctags misses.

    Catches TypedDict function-call style, Mongoose schemas, type aliases
    defined via assignment, etc. Filters out import/use/require lines and
    uses import_source path fragment for disambiguation when multiple
    matches exist.
    """
    if "." in name:
        parent, leaf = name.rsplit(".", 1)
        # Look for "class Command" inside a file that also has "class Create"
        pattern = rf"(class|interface|type|struct|enum|record)\s+{re.escape(leaf)}\b"
    else:
        pattern = (
            rf"(class|interface|type|struct|enum|record)\s+{re.escape(name)}\b"
            rf"|{re.escape(name)}\s*="
        )

    include_args = []
    for ext in _SOURCE_EXTENSIONS:
        include_args.extend(["--include", f"*{ext}"])

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


def resolve_type(
    name: str,
    import_source: str | None,
    ctags_index: dict[str, list[CtagsEntry]],
    project_root: Path,
) -> Path | None:
    """Resolve a type name to its source file. Language-agnostic.

    Two-tier resolution:
      1. ctags (primary) — with import_source for disambiguation
      2. grep (fallback) — for dynamic definitions ctags misses

    import_source is never used as a standalone file resolver.
    """
    path = resolve_from_ctags(name, import_source, ctags_index)
    if path is not None:
        return path
    return resolve_by_grep(name, project_root, import_source)


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
