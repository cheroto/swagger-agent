"""CLI entry point: python -m swagger_agent <target_dir> [-o output.yaml] [--dump-json result.json] [-v]"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

from swagger_agent.config import LLMConfig
from swagger_agent.pipeline import run_pipeline
from swagger_agent.telemetry import Telemetry


def _resolve_output_dir(target_dir: str) -> Path:
    """Determine a smart output directory under outputs/<repo_name>.

    If the directory already exists, appends a numeric suffix to avoid
    overwriting previous runs: outputs/my-repo, outputs/my-repo_2, etc.
    """
    repo_name = Path(target_dir).resolve().name
    base = Path("outputs") / repo_name

    if not base.exists():
        return base

    # Find next available suffix
    n = 2
    while True:
        candidate = Path("outputs") / f"{repo_name}_{n}"
        if not candidate.exists():
            return candidate
        n += 1


def _fmt_chars(n: int) -> str:
    """Format character count as human-readable size."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _print_telemetry(telemetry: Telemetry, console: Console) -> None:
    """Print LLM call telemetry as a summary table."""
    calls = telemetry.calls
    if not calls:
        return

    table = Table(title="LLM Calls", expand=True)
    table.add_column("#", width=3, justify="right", style="dim")
    table.add_column("Agent", min_width=10)
    table.add_column("Phase", min_width=12)
    table.add_column("Target", min_width=20)
    table.add_column("Input", width=8, justify="right")
    table.add_column("Output", width=8, justify="right")
    table.add_column("Time", width=8, justify="right")

    for i, c in enumerate(calls, 1):
        # Shorten target_file to basename
        target = os.path.basename(c.target_file) if c.target_file else ""
        table.add_row(
            str(i),
            c.agent,
            c.phase,
            target,
            _fmt_chars(c.input_chars),
            _fmt_chars(c.output_chars),
            f"{c.duration_ms / 1000:.1f}s",
        )

    # Totals row
    total_input = sum(c.input_chars for c in calls)
    total_output = sum(c.output_chars for c in calls)
    total_time = sum(c.duration_ms for c in calls)
    table.add_row(
        "", "[bold]TOTAL[/bold]", f"[bold]{len(calls)} calls[/bold]", "",
        f"[bold]{_fmt_chars(total_input)}[/bold]",
        f"[bold]{_fmt_chars(total_output)}[/bold]",
        f"[bold]{total_time / 1000:.1f}s[/bold]",
    )

    console.print(table)
    console.print()


def _print_completeness(completeness, console: Console) -> None:
    """Print the completeness checklist as a styled table.

    Uses contextual severity (critical / warning / info) instead of
    blanket PASS/FAIL, since not every unchecked item is an error.
    """
    # (check_key, label, severity when False)
    # "critical" = red, "warning" = yellow, "info" = dim
    CHECKS = [
        ("has_endpoints",        "Endpoints discovered",     "critical"),
        ("has_security_schemes", "Security schemes defined", "warning"),
        ("endpoints_have_auth",  "Endpoints have auth",      "warning"),
        ("has_error_responses",  "Error responses (4xx)",     "info"),
        ("has_request_bodies",   "Request bodies",            "info"),
        ("has_schemas",          "Schemas extracted",         "warning"),
        ("no_unresolved_refs",   "All $refs resolved",       "warning"),
        ("has_servers",          "Server URLs",               "info"),
        ("route_coverage",       "Route coverage",            None),
    ]

    data = completeness.model_dump()

    table = Table(title="Spec Completeness", expand=True)
    table.add_column("Check", min_width=28)
    table.add_column("Result", width=12, justify="center")

    for key, label, severity in CHECKS:
        value = data[key]

        if isinstance(value, float):
            pct = f"{value:.0%}"
            if value >= 1.0:
                table.add_row(label, f"[green]{pct}[/green]")
            elif value > 0:
                table.add_row(label, f"[yellow]{pct}[/yellow]")
            else:
                table.add_row(label, f"[red]{pct}[/red]")
        elif value:
            table.add_row(label, "[green]  ✓[/green]")
        else:
            icon_style = {"critical": "red", "warning": "yellow", "info": "dim"}.get(severity, "dim")
            icon = {"critical": "  ✗", "warning": "  —", "info": "  —"}.get(severity, "  —")
            table.add_row(f"[{icon_style}]{label}[/{icon_style}]", f"[{icon_style}]{icon}[/{icon_style}]")

    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="swagger-agent",
        description="Generate an OpenAPI 3.0 spec from a codebase using multi-agent extraction",
    )
    parser.add_argument("target_dir", help="Path to the target project directory")
    parser.add_argument("-o", "--output", metavar="PATH", help="Write YAML to file instead of stdout")
    parser.add_argument("--dump-json", metavar="PATH", help="Save full pipeline result as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable live dashboard")
    args = parser.parse_args()

    # Validate target
    if not os.path.isdir(args.target_dir):
        print(f"Error: directory not found: {args.target_dir}", file=sys.stderr)
        sys.exit(1)

    console = Console(stderr=True)
    config = LLMConfig()

    console.print(f"[dim]LLM: {config.llm_base_url} / {config.llm_model}[/dim]")
    console.print(f"[dim]Target: {os.path.abspath(args.target_dir)}[/dim]")
    console.print()

    # Use live dashboard when stderr is a TTY (unless --no-dashboard)
    use_dashboard = sys.stderr.isatty() and not args.no_dashboard
    dashboard = None

    if use_dashboard:
        from swagger_agent.dashboard import PipelineDashboard
        dashboard = PipelineDashboard(console=console)
        dashboard.start()

    try:
        result = run_pipeline(
            args.target_dir, config=config, console=console, dashboard=dashboard,
        )
    except BaseException:
        if dashboard:
            dashboard.stop()
        raise

    # Show completeness inside the dashboard before stopping it
    if dashboard:
        dashboard.set_completeness(result.completeness)
        import time
        time.sleep(0.5)  # Let the final refresh render
        dashboard.stop()
    else:
        # No dashboard — print completeness table to console
        console.print()
        _print_completeness(result.completeness, console)
        console.print()

    # Print LLM telemetry summary
    _print_telemetry(result.telemetry, console)

    # Print timings
    if args.verbose:
        console.print("[bold]Timings:[/bold]")
        for step, ms in result.timings.items():
            if step != "total":
                console.print(f"  {step}: {ms / 1000:.1f}s")
        console.print()

    # Print failed routes
    if result.failed_routes:
        console.print("[bold yellow]Failed route files:[/bold yellow]")
        for file, error in result.failed_routes:
            console.print(f"  {file}: {error}")
        console.print()

    # ── Smart output: default to outputs/<repo_name>/ ──────────────────
    output_dir = None
    yaml_path = args.output
    json_path = args.dump_json

    if not yaml_path and not json_path:
        # Default: write both YAML and JSON to outputs/<repo_name>/
        output_dir = _resolve_output_dir(args.target_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        yaml_path = str(output_dir / "openapi.yaml")
        json_path = str(output_dir / "result.json")

    # Output YAML
    if yaml_path:
        parent = os.path.dirname(yaml_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(yaml_path, "w") as f:
            f.write(result.yaml_str)
        console.print(f"[bold green]Spec written to {yaml_path}[/bold green]")

    # Dump JSON
    if json_path:
        parent = os.path.dirname(json_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        dump = {
            "spec": result.spec,
            "manifest": result.manifest.model_dump() if result.manifest else None,
            "descriptors": [d.model_dump(by_alias=True) for d in result.descriptors],
            "schemas": result.schemas,
            "completeness": result.completeness.model_dump(),
            "validation": asdict(result.validation),
            "failed_routes": result.failed_routes,
            "timings": result.timings,
            "telemetry": result.telemetry.summary(),
        }
        with open(json_path, "w") as f:
            json.dump(dump, f, indent=2, default=str)
        console.print(f"[dim]Full result written to {json_path}[/dim]")


if __name__ == "__main__":
    main()
