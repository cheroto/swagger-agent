"""CLI entry point: python -m swagger_agent <target_dir> [-o output.yaml] [--dump-json result.json] [-v]"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from rich.console import Console
from rich.table import Table

from swagger_agent.config import LLMConfig
from swagger_agent.pipeline import run_pipeline


def _print_completeness(completeness, console: Console) -> None:
    """Print the completeness checklist as a table."""
    table = Table(title="Completeness Checklist", expand=True)
    table.add_column("Check", min_width=25)
    table.add_column("Status", width=10, justify="center")

    checks = completeness.model_dump()
    for check, value in checks.items():
        label = check.replace("_", " ").title()
        if isinstance(value, float):
            pct = f"{value:.0%}"
            style = "green" if value >= 1.0 else "yellow" if value > 0 else "red"
            table.add_row(label, f"[{style}]{pct}[/{style}]")
        elif value:
            table.add_row(label, "[green]PASS[/green]")
        else:
            table.add_row(label, "[red]FAIL[/red]")

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

    # Run the pipeline
    result = run_pipeline(args.target_dir, config=config, console=console)

    # Print completeness table
    console.print()
    _print_completeness(result.completeness, console)
    console.print()

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

    # Output YAML
    if args.output:
        output_dir = os.path.dirname(args.output)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        with open(args.output, "w") as f:
            f.write(result.yaml_str)
        console.print(f"[bold green]Spec written to {args.output}[/bold green]")
    else:
        sys.stdout.write(result.yaml_str)

    # Dump JSON
    if args.dump_json:
        dump_dir = os.path.dirname(args.dump_json)
        if dump_dir:
            os.makedirs(dump_dir, exist_ok=True)

        dump = {
            "spec": result.spec,
            "manifest": result.manifest.model_dump() if result.manifest else None,
            "descriptors": [d.model_dump(by_alias=True) for d in result.descriptors],
            "schemas": result.schemas,
            "completeness": result.completeness.model_dump(),
            "validation": asdict(result.validation),
            "failed_routes": result.failed_routes,
            "timings": result.timings,
        }
        with open(args.dump_json, "w") as f:
            json.dump(dump, f, indent=2, default=str)
        console.print(f"[dim]Full result written to {args.dump_json}[/dim]")


if __name__ == "__main__":
    main()
