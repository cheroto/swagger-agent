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


def _print_telemetry_table(calls: list[dict], console: Console) -> None:
    """Print LLM call telemetry as a summary table.

    Accepts a list of call dicts (either from Telemetry.calls converted via
    to_dict(), or loaded directly from result.json telemetry.calls).
    """
    if not calls:
        console.print("[dim]No LLM calls recorded.[/dim]")
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
        target = os.path.basename(c.get("target_file", "")) if c.get("target_file") else ""
        table.add_row(
            str(i),
            c.get("agent", ""),
            c.get("phase", ""),
            target,
            _fmt_chars(c.get("input_chars", 0)),
            _fmt_chars(c.get("output_chars", 0)),
            f"{c.get('duration_ms', 0) / 1000:.1f}s",
        )

    # Totals row
    total_input = sum(c.get("input_chars", 0) for c in calls)
    total_output = sum(c.get("output_chars", 0) for c in calls)
    total_time = sum(c.get("duration_ms", 0) for c in calls)
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


def _is_git_url(target: str) -> bool:
    """Check if target looks like a Git URL."""
    return (
        target.startswith(("https://", "http://", "git@", "ssh://"))
        or (target.count("/") == 1 and not os.path.exists(target))  # owner/repo shorthand
    )


def _clone_repo(url: str, ref: str | None = None) -> str:
    """Clone a Git repo to a temp directory. Returns the cloned path."""
    import subprocess
    import tempfile

    # Expand owner/repo shorthand to GitHub URL
    if url.count("/") == 1 and not url.startswith(("https://", "http://", "git@", "ssh://")):
        url = f"https://github.com/{url}.git"

    # Derive repo name for the temp dir
    repo_name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    clone_dir = os.path.join(tempfile.gettempdir(), "swagger-agent", repo_name)

    if os.path.isdir(clone_dir):
        import shutil
        shutil.rmtree(clone_dir)

    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [url, clone_dir]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        if ref and "not found" in e.stderr:
            # Branch/tag not found — clone full and checkout
            subprocess.run(
                ["git", "clone", url, clone_dir],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "checkout", ref],
                check=True, capture_output=True, text=True,
                cwd=clone_dir,
            )
        else:
            print(f"Error cloning {url}: {e.stderr}", file=sys.stderr)
            sys.exit(1)

    return clone_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="swagger-agent",
        description="Generate an OpenAPI 3.0 spec from a codebase using multi-agent extraction",
    )
    parser.add_argument(
        "target", nargs="?",
        help="Local path or Git URL (GitHub/GitLab). URLs are cloned to a temp directory.",
    )
    parser.add_argument("--ref", metavar="REF", help="Git branch, tag, or commit to checkout after cloning")
    parser.add_argument("-o", "--output", metavar="PATH", help="Write YAML to file instead of stdout")
    parser.add_argument("--dump-json", metavar="PATH", help="Save full pipeline result as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable live dashboard")
    parser.add_argument(
        "--skip-scout", action="store_true",
        help="[experimental] Skip Scout LLM and use deterministic prescan as the discovery manifest",
    )
    parser.add_argument(
        "--telemetry", action="store_true",
        help="Print per-LLM-call telemetry table after the run",
    )
    parser.add_argument(
        "--telemetry-from", metavar="PATH",
        help="Print telemetry from a prior result.json and exit (no run)",
    )
    cache_group = parser.add_mutually_exclusive_group()
    cache_group.add_argument(
        "--no-cache", action="store_true",
        help="Disable LLM response cache (always call the LLM)",
    )
    cache_group.add_argument(
        "--overwrite-cache", action="store_true",
        help="Ignore existing cache entries and overwrite with fresh LLM responses",
    )
    parser.add_argument(
        "--clear-cache", action="store_true",
        help="Clear the LLM response cache and exit",
    )
    args = parser.parse_args()

    # -- Replay mode: just print telemetry from a prior result.json --
    if args.telemetry_from:
        console = Console(stderr=True)
        path = args.telemetry_from
        if not os.path.isfile(path):
            print(f"Error: file not found: {path}", file=sys.stderr)
            sys.exit(1)
        with open(path) as f:
            data = json.load(f)
        telemetry_data = data.get("telemetry")
        if not telemetry_data or not telemetry_data.get("calls"):
            console.print("[yellow]No telemetry data in this result.json[/yellow]")
            sys.exit(0)
        _print_telemetry_table(telemetry_data["calls"], console)
        sys.exit(0)

    # -- Clear cache mode --
    if args.clear_cache:
        from swagger_agent.cache import clear
        n = clear()
        print(f"Cleared {n} cached LLM response(s).")
        sys.exit(0)

    # -- Cache mode --
    if args.no_cache:
        from swagger_agent.config import set_cache_mode
        set_cache_mode("off")
    elif args.overwrite_cache:
        from swagger_agent.config import set_cache_mode
        set_cache_mode("overwrite")

    # Validate target
    if not args.target:
        parser.error("target is required (unless using --telemetry-from)")

    console = Console(stderr=True)

    # Clone remote repos
    cloned = False
    target_dir = args.target
    if _is_git_url(args.target):
        console.print(f"[dim]Cloning {args.target}...[/dim]")
        target_dir = _clone_repo(args.target, ref=args.ref)
        cloned = True
        console.print(f"[dim]Cloned to {target_dir}[/dim]")
    elif args.ref:
        parser.error("--ref can only be used with a Git URL")

    if not os.path.isdir(target_dir):
        print(f"Error: directory not found: {target_dir}", file=sys.stderr)
        sys.exit(1)

    config = LLMConfig()

    console.print(f"[dim]LLM: {config.llm_base_url} / {config.llm_model}[/dim]")
    console.print(f"[dim]Target: {os.path.abspath(target_dir)}[/dim]")
    if args.no_cache:
        console.print("[dim]Cache: disabled[/dim]")
    elif args.overwrite_cache:
        console.print("[dim]Cache: overwrite mode[/dim]")
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
            target_dir, config=config, console=console, dashboard=dashboard,
            skip_scout=args.skip_scout,
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

    # Print LLM telemetry summary (only with --telemetry flag)
    if args.telemetry:
        _print_telemetry_table(
            [c.to_dict() for c in result.telemetry.calls], console,
        )

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
        output_dir = _resolve_output_dir(target_dir)
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
