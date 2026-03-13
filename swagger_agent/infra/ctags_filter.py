"""Ctags-based route file prefiltering for Phase 2 input reduction.

Given a route file and the handler names identified by Phase 1, uses
universal-ctags to locate function/method boundaries and builds a
condensed view containing only:
  - Import/package/module declarations (top of file)
  - Class/interface declarations with their annotations/decorators
  - Function/method signatures with their annotations/decorators
  - Bodies replaced with empty blocks

This is language-agnostic: ctags handles parsing via its grammar files.
If ctags cannot parse the file or no handlers are matched, the original
file content is returned unchanged (safe fallback).
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from swagger_agent.infra.resolve import _find_ctags_binary

logger = logging.getLogger("swagger_agent.ctags_filter")


@dataclass
class _TagEntry:
    """A single ctags tag with start/end line numbers."""

    name: str
    kind: str  # "method", "function", "class", etc.
    line: int  # 1-based start line (the declaration line)
    end: int | None = None  # 1-based end line (closing brace)
    scope: str | None = None


@dataclass
class PrefilterResult:
    """Result of ctags prefiltering."""

    content: str  # The (possibly filtered) file content
    was_filtered: bool  # True if ctags filtering was applied
    original_chars: int
    filtered_chars: int
    matched_handlers: list[str] = field(default_factory=list)
    unmatched_handlers: list[str] = field(default_factory=list)
    reason: str = ""  # Why filtering was skipped (empty if filtered)


def _run_ctags_on_file(file_path: Path) -> list[_TagEntry]:
    """Run ctags on a single file, returning all tags with line ranges."""
    try:
        ctags_bin = _find_ctags_binary()
    except RuntimeError:
        return []

    cmd = [
        ctags_bin,
        "--output-format=json",
        "--fields=+Sne",  # S=signature, n=line, e=end
        "-f", "-",
        str(file_path),
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    tags: list[_TagEntry] = []
    for raw_line in result.stdout.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            tag = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        tags.append(_TagEntry(
            name=tag.get("name", ""),
            kind=tag.get("kind", ""),
            line=tag.get("line", 0),
            end=tag.get("end"),
            scope=tag.get("scope"),
        ))

    # Sort by line number
    tags.sort(key=lambda t: t.line)
    return tags


# Kinds that represent callable handlers (functions/methods)
_HANDLER_KINDS = frozenset({
    "method", "function", "member",  # covers most languages
})

# Kinds that represent structural containers (classes, interfaces, etc.)
_CONTAINER_KINDS = frozenset({
    "class", "interface", "struct", "object", "module",
})

# Kinds for imports/packages
_PREAMBLE_KINDS = frozenset({
    "package", "import", "using",
})


def _find_preamble_end(lines: list[str], tags: list[_TagEntry]) -> int:
    """Find where the file preamble (imports, package decl) ends.

    Returns the 0-based line index of the last preamble line.
    Uses a simple heuristic: the preamble is everything before the first
    class/function/method declaration, or before the first tag that isn't
    an import/package.
    """
    first_code_line = len(lines)  # default: entire file is preamble

    for tag in tags:
        if tag.kind not in _PREAMBLE_KINDS:
            # This tag is code — preamble ends before it.
            # But we also need to include any decorators/annotations above it,
            # so we walk backwards from tag.line to find them.
            first_code_line = tag.line - 1  # convert to 0-based
            break

    return first_code_line


def _find_decorator_start(lines: list[str], decl_line_0based: int) -> int:
    """Walk backwards from a declaration to find decorator/annotation lines.

    Returns the 0-based index of the first decorator/annotation line.
    Handles: @Decorator (Java/Python/TS), [Attribute] (C#), #[attr] (Rust).
    """
    i = decl_line_0based - 1
    while i >= 0:
        stripped = lines[i].strip()
        if not stripped:
            # blank line — skip, decorators might have gaps
            i -= 1
            continue
        if (
            stripped.startswith("@")
            or stripped.startswith("[")  # C# attributes
            or stripped.startswith("#[")  # Rust attributes
            or stripped.startswith("//")  # single-line comments (might be doc)
            or stripped.startswith("/*")  # block comment start
            or stripped.startswith("*")  # block comment continuation
            or stripped.startswith("*/")  # block comment end
            or stripped.startswith("/**")  # javadoc
            or stripped.startswith("///")  # Rust/C# doc comments
        ):
            i -= 1
            continue
        # Hit a non-decorator, non-blank line — stop
        break

    return i + 1


def _find_signature_end(lines: list[str], decl_line_0based: int) -> int:
    """Find the end of a function/method signature that may span multiple lines.

    Scans forward from the declaration line until we find a line containing
    an opening brace '{' (or a colon ':' for Python), which marks the start
    of the body. Returns the 0-based index of that line.
    """
    for i in range(decl_line_0based, min(len(lines), decl_line_0based + 10)):
        stripped = lines[i].rstrip()
        if stripped.endswith("{") or stripped.endswith(":"):
            return i
        # Also handle cases like `) {` or `) throws Exception {`
        if "{" in stripped:
            return i
    # Couldn't find body start — just return the declaration line
    return decl_line_0based


def prefilter_route_file(
    file_path: str,
    file_content: str,
    handler_names: list[str],
) -> PrefilterResult:
    """Build a condensed view of a route file for Phase 2.

    Args:
        file_path: Path to the route file.
        file_content: Full file content (already read).
        handler_names: Handler/function names from Phase 1 EndpointSketches.

    Returns:
        PrefilterResult with filtered or original content.
    """
    original_chars = len(file_content)

    if not handler_names:
        return PrefilterResult(
            content=file_content,
            was_filtered=False,
            original_chars=original_chars,
            filtered_chars=original_chars,
            reason="no handler names from Phase 1",
        )

    # Run ctags
    tags = _run_ctags_on_file(Path(file_path))
    if not tags:
        return PrefilterResult(
            content=file_content,
            was_filtered=False,
            original_chars=original_chars,
            filtered_chars=original_chars,
            reason="ctags produced no tags for this file",
        )

    lines = file_content.splitlines()
    handler_set = set(handler_names)

    # Find ALL handler-kind tags in the file (not just Phase 1 matches).
    # We include every function/method signature — the goal is to strip
    # bodies, not to exclude unrecognized methods. Phase 1 match info
    # is for reporting only.
    all_handler_tags: list[_TagEntry] = []
    matched_names: list[str] = []
    for tag in tags:
        if tag.kind in _HANDLER_KINDS:
            all_handler_tags.append(tag)
            if tag.name in handler_set:
                matched_names.append(tag.name)

    unmatched = [n for n in handler_names if n not in set(matched_names)]

    if not all_handler_tags:
        return PrefilterResult(
            content=file_content,
            was_filtered=False,
            original_chars=original_chars,
            filtered_chars=original_chars,
            unmatched_handlers=unmatched,
            reason=f"no function/method tags found by ctags "
                   f"(ctags kinds: {sorted(set(t.kind for t in tags))})",
        )

    # Build the condensed view
    # 1. Preamble: everything up to the first non-import tag
    preamble_end = _find_preamble_end(lines, tags)

    # Collect line ranges to include (0-based, inclusive)
    include_ranges: list[tuple[int, int]] = []

    # Always include preamble (imports, package)
    if preamble_end > 0:
        include_ranges.append((0, preamble_end - 1))

    # 2. Container declarations (class, interface) — include declaration
    #    line + decorators, but not their body (handlers fill that)
    for tag in tags:
        if tag.kind in _CONTAINER_KINDS:
            decl_line = tag.line - 1  # 0-based
            decorator_start = _find_decorator_start(lines, decl_line)
            # Include decorators + the declaration line only
            include_ranges.append((decorator_start, decl_line))

    # 3. ALL functions/methods — include decorators + full signature, skip body.
    #    This ensures no handler is excluded even if Phase 1 misspelled its name.
    for tag in all_handler_tags:
        decl_line = tag.line - 1  # 0-based
        decorator_start = _find_decorator_start(lines, decl_line)
        sig_end = _find_signature_end(lines, decl_line)
        # Include decorators + full multi-line signature
        include_ranges.append((decorator_start, sig_end))

    # Merge overlapping ranges and sort
    include_ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in include_ranges:
        if merged and start <= merged[-1][1] + 2:
            # Merge if adjacent or overlapping (allow 1-line gap for blank lines)
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # Build output
    output_parts: list[str] = []
    for start, end in merged:
        clamped_start = max(0, start)
        clamped_end = min(len(lines) - 1, end)
        chunk = lines[clamped_start:clamped_end + 1]
        output_parts.append("\n".join(chunk))

    # Add closing brace if the file likely has a class wrapper
    # (check if last line of original content has a closing brace)
    stripped_last = lines[-1].strip() if lines else ""
    if stripped_last == "}" and not output_parts[-1].strip().endswith("}"):
        output_parts.append("}")

    filtered_content = "\n\n".join(output_parts)
    filtered_chars = len(filtered_content)

    # Check if we actually saved anything meaningful (at least 10%)
    if filtered_chars >= original_chars * 0.9:
        return PrefilterResult(
            content=file_content,
            was_filtered=False,
            original_chars=original_chars,
            filtered_chars=original_chars,
            matched_handlers=matched_names,
            unmatched_handlers=unmatched,
            reason=f"filtering saved less than 10% "
                   f"({original_chars} → {filtered_chars} chars)",
        )

    return PrefilterResult(
        content=filtered_content,
        was_filtered=True,
        original_chars=original_chars,
        filtered_chars=filtered_chars,
        matched_handlers=matched_names,
        unmatched_handlers=unmatched,
    )
