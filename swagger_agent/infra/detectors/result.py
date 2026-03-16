"""PrescanResult dataclass and scratchpad formatter."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PrescanResult:
    framework: str | None = None
    language: str | None = None
    route_files: list[str] = field(default_factory=list)
    servers: list[str] = field(default_factory=list)
    base_path: str = ""
    auth_context_hint: str = ""
    notes: list[str] = field(default_factory=list)


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
