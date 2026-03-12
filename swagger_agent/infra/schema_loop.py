"""Schema resolution loop — prototype of the Ref Resolver infrastructure.

Starting from ref_hints in an endpoint descriptor (or a route file), resolves
imports to source files, runs the Schema Extractor iteratively, and follows
$ref chains until all reachable schemas are extracted.

Usage:
    # From endpoint descriptor JSON (output of route extractor)
    python -m swagger_agent.infra.schema_loop \
      --descriptor endpoint_descriptor.json \
      --project-root /path/to/repo \
      --framework spring

    # From a route file (runs route extractor first, then resolves schemas)
    python -m swagger_agent.infra.schema_loop \
      --route-file /path/to/controller.java \
      --project-root /path/to/repo \
      --framework spring --base-path /api

    # Dump all collected schemas
    python -m swagger_agent.infra.schema_loop \
      --descriptor endpoint_descriptor.json \
      --project-root /path/to/repo \
      --framework spring --dump-json schemas_output.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import deque
from pathlib import Path

from rich.console import Console
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table

from swagger_agent.config import LLMConfig
from swagger_agent.infra.resolve import (
    build_ctags_index,
    resolve_from_ctags,
    resolve_by_grep,
    scan_refs_in_schemas,
)
from swagger_agent.agents.schema_extractor.harness import (
    run_schema_extractor,
    SchemaExtractorContext,
)
from swagger_agent.models import EndpointDescriptor, SchemaDescriptor


def collect_ref_hints_from_descriptor(descriptor: EndpointDescriptor) -> list[dict]:
    """Extract all ref_hints from an endpoint descriptor."""
    hints: list[dict] = []
    seen: set[str] = set()

    for ep in descriptor.endpoints:
        refs = []
        if ep.request_body and ep.request_body.schema_ref:
            refs.append(ep.request_body.schema_ref)
        for resp in ep.responses:
            if resp.schema_ref:
                refs.append(resp.schema_ref)

        for ref in refs:
            if ref.ref_hint not in seen and ref.resolution != "unresolvable":
                seen.add(ref.ref_hint)
                hints.append(ref.model_dump(by_alias=True))

    return hints


def run_schema_loop(
    ref_hints: list[dict],
    framework: str,
    project_root: Path,
    config: LLMConfig | None = None,
    console: Console | None = None,
    max_depth: int = 10,
) -> dict[str, dict]:
    """Run the schema extraction loop until all $refs are resolved.

    Args:
        ref_hints: Initial ref_hints from endpoint descriptors.
        framework: Framework name.
        project_root: Root of the target project.
        config: LLM config.
        console: Rich console for output.
        max_depth: Max recursion depth (safety net).

    Returns:
        Dict of all collected schemas: {"SchemaName": {JSON Schema}, ...}
    """
    console = console or Console(stderr=True)
    config = config or LLMConfig()

    # Build ctags index once before the loop
    console.print("[dim]Building ctags index...[/dim]")
    ctags_index = build_ctags_index(project_root)
    console.print(f"[dim]Indexed {sum(len(v) for v in ctags_index.values())} type definitions[/dim]")

    all_schemas: dict[str, dict] = {}      # Accumulated schemas
    extracted_files: set[str] = set()       # Files already processed
    queue: deque[tuple[str, str | None]] = deque()

    # 1. Seed the queue from ref_hints
    for hint in ref_hints:
        name = hint["ref_hint"]
        import_source = hint.get("import_source")
        if name not in all_schemas:
            queue.append((name, import_source))

    depth = 0
    while queue and depth < max_depth:
        depth += 1
        batch_size = len(queue)
        console.print(Rule(f" Resolution Round {depth} ({batch_size} pending) ", style="bold yellow"))

        # Process all items in the current batch
        round_new_schemas: dict[str, dict] = {}
        round_items = [queue.popleft() for _ in range(batch_size)]

        for schema_name, import_source in round_items:
            if schema_name in all_schemas:
                continue

            # Resolve via ctags, fall back to grep
            file_path = resolve_from_ctags(schema_name, import_source, ctags_index)
            if file_path is None:
                file_path = resolve_by_grep(schema_name, project_root)

            if file_path is None:
                console.print(f"  [red]Could not resolve[/red] {schema_name}"
                              f" (import: {import_source or 'none'})")
                all_schemas[schema_name] = {
                    "type": "object",
                    "description": "Schema could not be resolved from source code.",
                    "x-unresolved": True,
                }
                continue

            file_key = str(file_path)
            if file_key in extracted_files:
                console.print(f"  [dim]Already extracted {file_path.name}, skipping[/dim]")
                continue

            console.print(f"  [bold]{schema_name}[/bold] → {file_path.relative_to(project_root)}")

            # Build known_schemas context: schemas whose names appear in this file
            file_text = file_path.read_text(encoding="utf-8", errors="replace")
            known_schemas = {
                n: all_schemas[n] for n in all_schemas
                if n in file_text and not all_schemas[n].get("x-unresolved")
            }

            # Run schema extractor
            context = SchemaExtractorContext(
                framework=framework,
                target_file=str(file_path),
                known_schemas=known_schemas,
            )

            try:
                descriptor, record = run_schema_extractor(
                    str(file_path), context, config=config,
                )
                console.print(
                    f"    Extracted {record.schema_count} schema(s) in "
                    f"{record.duration_ms:.0f}ms"
                )
                round_new_schemas.update(descriptor.schemas)
                extracted_files.add(file_key)
            except Exception as e:
                console.print(f"    [red]Extraction failed:[/red] {e}")
                all_schemas[schema_name] = {
                    "type": "object",
                    "description": f"Schema extraction failed: {e}",
                    "x-unresolved": True,
                }

        # Merge new schemas
        all_schemas.update(round_new_schemas)

        # Scan for new $ref targets not yet resolved
        new_refs = scan_refs_in_schemas(round_new_schemas)
        unresolved = new_refs - set(all_schemas.keys())

        if unresolved:
            console.print(
                f"\n  [cyan]New $refs discovered:[/cyan] {', '.join(sorted(unresolved))}"
            )
            for ref_name in unresolved:
                queue.append((ref_name, None))
        else:
            console.print(f"\n  [green]No new unresolved $refs[/green]")

    if queue:
        console.print(f"\n[yellow]Stopped after {max_depth} rounds. "
                       f"{len(queue)} refs still pending.[/yellow]")
        for name, _import_source in queue:
            if name not in all_schemas:
                all_schemas[name] = {
                    "type": "object",
                    "description": "Schema resolution exceeded max depth.",
                    "x-unresolved": True,
                }

    return all_schemas


def print_schema_summary(schemas: dict[str, dict], console: Console) -> None:
    """Print a summary table of all collected schemas."""
    console.print(Rule(" All Collected Schemas ", style="bold green"))

    table = Table(expand=True)
    table.add_column("Schema", min_width=20, style="bold")
    table.add_column("Properties", width=12, justify="right")
    table.add_column("Required", width=10, justify="right")
    table.add_column("$refs", width=8, justify="right")
    table.add_column("Status", width=12)

    for name, schema in sorted(schemas.items()):
        if schema.get("x-unresolved"):
            table.add_row(name, "-", "-", "-", "[red]unresolved[/red]")
            continue

        props = schema.get("properties", {})
        required = schema.get("required", [])
        ref_count = len(scan_refs_in_schemas({name: schema}))

        table.add_row(
            name,
            str(len(props)),
            str(len(required)),
            str(ref_count) if ref_count else "[dim]0[/dim]",
            "[green]ok[/green]",
        )

    console.print(table)
    console.print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the schema resolution loop — resolve all $refs from endpoints",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--descriptor", metavar="PATH",
        help="Path to endpoint descriptor JSON (route extractor output)",
    )
    source.add_argument(
        "--route-file", metavar="PATH",
        help="Path to a route file (runs route extractor first)",
    )

    parser.add_argument("--project-root", required=True, help="Root of the target project")
    parser.add_argument("--framework", required=True, help="Framework name")
    parser.add_argument("--base-path", default="", help="API base path (for --route-file mode)")
    parser.add_argument("--max-depth", type=int, default=10, help="Max resolution rounds")
    parser.add_argument("--dump-json", metavar="PATH", help="Save all schemas to JSON file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show full JSON output")
    args = parser.parse_args()

    console = Console(stderr=True)
    config = LLMConfig()

    project_root = Path(args.project_root).resolve()
    if not project_root.is_dir():
        print(f"Error: project root not found: {args.project_root}", file=sys.stderr)
        sys.exit(1)

    console.print(f"[dim]LLM: {config.llm_base_url} / {config.llm_model}[/dim]")
    console.print(f"[dim]Project root: {project_root}[/dim]")
    console.print(f"[dim]Framework: {args.framework}[/dim]")
    console.print()

    # Get endpoint descriptor
    if args.descriptor:
        if not os.path.isfile(args.descriptor):
            print(f"Error: descriptor not found: {args.descriptor}", file=sys.stderr)
            sys.exit(1)
        with open(args.descriptor) as f:
            data = json.load(f)
        # Support both raw descriptor and run-record wrapper
        desc_data = data.get("descriptor", data)
        descriptor = EndpointDescriptor.model_validate(desc_data)
        console.print(f"[dim]Loaded descriptor: {len(descriptor.endpoints)} endpoints[/dim]")
    else:
        # Run route extractor first
        from swagger_agent.agents.route_extractor.harness import (
            run_route_extractor,
            RouteExtractorContext,
        )
        from swagger_agent.agents.route_extractor.rich_output import (
            print_extraction_summary,
            print_endpoints_table,
        )

        route_file = os.path.abspath(args.route_file)
        if not os.path.isfile(route_file):
            print(f"Error: route file not found: {args.route_file}", file=sys.stderr)
            sys.exit(1)

        console.print(Rule(" Route Extraction ", style="bold blue"))
        route_ctx = RouteExtractorContext(
            framework=args.framework,
            base_path=args.base_path,
            target_file=route_file,
        )
        descriptor, route_record = run_route_extractor(route_file, route_ctx, config=config)
        print_extraction_summary(route_record, console)
        print_endpoints_table(descriptor, console)

    # Collect ref_hints
    ref_hints = collect_ref_hints_from_descriptor(descriptor)
    if not ref_hints:
        console.print("[yellow]No resolvable ref_hints found in descriptor.[/yellow]")
        sys.exit(0)

    console.print(
        f"[bold]Starting schema resolution:[/bold] "
        f"{len(ref_hints)} initial ref(s): "
        f"{', '.join(h['ref_hint'] for h in ref_hints)}"
    )
    console.print()

    # Run the loop
    all_schemas = run_schema_loop(
        ref_hints=ref_hints,
        framework=args.framework,
        project_root=project_root,
        config=config,
        console=console,
        max_depth=args.max_depth,
    )

    # Summary
    print_schema_summary(all_schemas, console)

    resolved = sum(1 for s in all_schemas.values() if not s.get("x-unresolved"))
    unresolved = sum(1 for s in all_schemas.values() if s.get("x-unresolved"))
    console.print(f"[bold]Total:[/bold] {len(all_schemas)} schemas "
                  f"({resolved} resolved, {unresolved} unresolved)")

    if args.verbose:
        console.print(Rule(" Full Schema Output ", style="bold cyan"))
        console.print(Syntax(
            json.dumps(all_schemas, indent=2, default=str),
            "json", theme="monokai", line_numbers=False,
        ))

    if args.dump_json:
        dump_dir = os.path.dirname(args.dump_json)
        if dump_dir:
            os.makedirs(dump_dir, exist_ok=True)
        with open(args.dump_json, "w") as f:
            json.dump(all_schemas, f, indent=2, default=str)
        console.print(f"[dim]Schemas written to {args.dump_json}[/dim]")


if __name__ == "__main__":
    main()
