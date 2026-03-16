"""Schema resolution loop — resolves ref_hints and extracts schemas iteratively.

Starting from ref_hints in endpoint descriptors, resolves imports to source
files, runs the Schema Extractor iteratively, and follows $ref chains until
all reachable schemas are extracted.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
import sys
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Console
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table

from swagger_agent.config import LLMConfig
from swagger_agent.infra.assembler import _sanitize_ref_hint
from swagger_agent.infra.resolve import (
    CtagsEntry,
    build_ctags_index,
    build_inheritance_map,
    resolve_type,
    scan_refs_in_schemas,
)
from swagger_agent.agents.schema_extractor.harness import (
    run_schema_extractor,
    SchemaExtractorContext,
)
from swagger_agent.models import EndpointDescriptor, SchemaDescriptor
from swagger_agent.telemetry import Telemetry

from .type_hints import _decompose_type_hint

logger = logging.getLogger("swagger_agent.schema_loop")


def _compute_qualified_names(
    name: str,
    file_paths: list[Path],
    project_root: Path,
) -> dict[str, str]:
    """Compute unique qualified names for a type name that resolves to multiple files.

    Finds the shortest parent directory component that disambiguates
    all files, then prefixes the type name with it.

    Returns: {str(file_path) → qualified_name}
    """
    rels = []
    for fp in file_paths:
        try:
            rel = fp.relative_to(project_root)
        except ValueError:
            rel = fp
        # Directory components only (exclude filename)
        rels.append((str(fp), list(rel.parts[:-1])))

    max_depth = max((len(parts) for _, parts in rels), default=1)

    for depth in range(1, max_depth + 1):
        components: dict[str, list[str]] = defaultdict(list)
        for fk, parts in rels:
            if len(parts) >= depth:
                comp = parts[-depth]
            else:
                comp = "_".join(parts) if parts else "unknown"
            components[comp].append(fk)

        if all(len(fks) == 1 for fks in components.values()):
            result = {}
            for comp, fks in components.items():
                clean = re.sub(r"[^a-zA-Z0-9_]", "", comp)
                result[fks[0]] = f"{clean}_{name}" if clean else name
            return result

    # Fallback: use full relative path as qualifier
    result = {}
    for fk, parts in rels:
        clean = "_".join(re.sub(r"[^a-zA-Z0-9_]", "", p) for p in parts)
        result[fk] = f"{clean}_{name}" if clean else name
    return result


def collect_ref_hints_from_descriptor(descriptor: EndpointDescriptor) -> list[dict]:
    """Extract all ref_hints from an endpoint descriptor.

    Sanitizes ref_hint values (strips stale $ref prefixes like
    '#/components/schemas/') before returning.  Each hint is tagged with
    ``_source_file`` so collision detection can trace back to the
    originating descriptor.

    Deduplicates by (name, import_line, file_namespace) so that the same
    type name from different import contexts survives as separate hints.
    """
    hints: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for ep in descriptor.endpoints:
        refs = []
        if ep.request_body and ep.request_body.schema_ref:
            refs.append(ep.request_body.schema_ref)
        for resp in ep.responses:
            if resp.schema_ref and not resp.schema_ref.is_empty:
                refs.append(resp.schema_ref)

        for ref in refs:
            clean_name = _sanitize_ref_hint(ref.ref_hint)
            key = (clean_name, ref.import_line, ref.file_namespace)
            if key not in seen:
                seen.add(key)
                hint = ref.model_dump(by_alias=True)
                hint["ref_hint"] = clean_name
                hint["_source_file"] = descriptor.source_file
                hints.append(hint)

    return hints


def run_schema_loop(
    ref_hints: list[dict],
    framework: str,
    project_root: Path,
    config: LLMConfig | None = None,
    console: Console | None = None,
    max_depth: int = 10,
    event_callback: object | None = None,
    telemetry: Telemetry | None = None,
    inline_schemas: dict[str, dict] | None = None,
) -> tuple[dict[str, dict], dict, dict[tuple[str, str], str]]:
    """Run the schema extraction loop until all $refs are resolved.

    Returns:
        Tuple of (schemas dict, inheritance map, name_mapping).
        name_mapping maps (original_ref_hint, source_file) → qualified schema name.
        It is non-empty only when name collisions required qualification.
    """
    console = console or Console(stderr=True)
    config = config or LLMConfig()
    _emit = event_callback or (lambda event, **kw: None)
    quiet = event_callback is not None

    # Build ctags index once before the loop
    if not quiet:
        console.print("[dim]Building ctags index...[/dim]")
    ctags_index = build_ctags_index(project_root)
    type_count = sum(len(v) for v in ctags_index.values())
    if not quiet:
        console.print(f"[dim]Indexed {type_count} type definitions[/dim]")
    _emit("ctags_built", count=type_count)

    # Build inheritance map for subtype discovery
    inheritance_map = build_inheritance_map(ctags_index)
    if inheritance_map and not quiet:
        total_children = sum(len(v) for v in inheritance_map.values())
        console.print(f"[dim]Inheritance map: {len(inheritance_map)} base types, {total_children} subtypes[/dim]")

    all_schemas: dict[str, dict] = {}
    extracted_files: set[str] = set()
    queue: deque[tuple[str, str | None]] = deque()

    # Maps qualified names back to (original_name, import_source) for resolution
    qualified_context: dict[str, tuple[str, str | None]] = {}
    # Maps (original_ref_hint, source_descriptor_file) → qualified schema name
    name_mapping: dict[tuple[str, str], str] = {}

    # Inline schemas from endpoint descriptors — used to build richer
    # placeholders instead of bare {type: object} when resolution fails.
    inline_schemas = inline_schemas or {}
    # Track LLM's resolution classification per ref name — if the LLM said
    # "unresolvable" and ctags confirms, no warning needed (expected behavior).
    llm_resolution_by_name: dict[str, str] = {}

    # 1. Pre-resolve ref_hints to detect name collisions
    #    When the same type name resolves to different files from different
    #    import contexts, each gets a unique qualified name.
    hint_triples: list[tuple[str, str | None, str]] = []
    for hint in ref_hints:
        raw_name = _sanitize_ref_hint(hint["ref_hint"])
        import_source = (
            hint.get("import_line")
            or hint.get("file_namespace")
            or hint.get("import_source")
            or None
        )
        source_file = hint.get("_source_file", "")

        resolution = hint.get("resolution", "")
        if resolution:
            llm_resolution_by_name.setdefault(raw_name, resolution)

        inner_names = _decompose_type_hint(raw_name)
        if not inner_names:
            logger.info("Skipping builtin type hint: %s", raw_name)
        for name in inner_names:
            hint_triples.append((name, import_source, source_file))

    # Pre-resolve each hint to its file path
    name_to_files: dict[str, dict[str | None, str | None]] = defaultdict(dict)
    triple_resolutions: dict[tuple[str, str | None, str], str | None] = {}
    for name, import_source, source_file in hint_triples:
        file_path = resolve_type(name, import_source, ctags_index, project_root)
        file_key = str(file_path) if file_path else None
        name_to_files[name].setdefault(file_key, import_source)
        triple_resolutions[(name, import_source, source_file)] = file_key

    # Detect collisions: same name resolving to multiple distinct files
    collision_quals: dict[str, dict[str, str]] = {}
    for name, file_dict in name_to_files.items():
        real_files = {k: v for k, v in file_dict.items() if k is not None}
        if len(real_files) > 1:
            qual_map = _compute_qualified_names(
                name, [Path(k) for k in real_files], project_root,
            )
            collision_quals[name] = qual_map
            if not quiet:
                console.print(f"  [yellow]Name collision:[/yellow] {name} → {len(real_files)} files")
                for fk, qn in qual_map.items():
                    console.print(f"    {qn} ← {Path(fk).relative_to(project_root)}")
            _emit("collision", name=name, qualified_names=list(qual_map.values()))

    # Seed queue with (possibly qualified) names
    seeded: set[str] = set()
    for name, import_source, source_file in hint_triples:
        if name in collision_quals:
            file_key = triple_resolutions.get((name, import_source, source_file))
            if file_key and file_key in collision_quals[name]:
                qualified = collision_quals[name][file_key]
            else:
                qualified = name
            name_mapping[(name, source_file)] = qualified
            if qualified != name:
                qualified_context[qualified] = (name, import_source)
        else:
            qualified = name
            name_mapping[(name, source_file)] = name

        if qualified not in seeded and qualified not in all_schemas:
            queue.append((qualified, import_source))
            seeded.add(qualified)

    depth = 0
    while queue and depth < max_depth:
        depth += 1
        batch_size = len(queue)
        if not quiet:
            console.print(Rule(f" Resolution Round {depth} ({batch_size} pending) ", style="bold yellow"))
        _emit("round_start", round=depth, pending=batch_size)

        round_new_schemas: dict[str, dict] = {}
        round_items = [queue.popleft() for _ in range(batch_size)]

        # Phase A: Resolve type names to file paths
        extraction_tasks: list[tuple[str, Path, dict[str, dict]]] = []
        round_queued_files: set[str] = set()
        schemas_snapshot = dict(all_schemas)
        skipped_dotted: list[str] = []  # dotted names that hit "already extracted"

        for schema_name, import_source in round_items:
            if schema_name in all_schemas:
                continue

            # For qualified names from collision resolution, resolve using the
            # original name and import context
            resolve_name = schema_name
            if schema_name in qualified_context:
                resolve_name, import_source = qualified_context[schema_name]

            inner_names = _decompose_type_hint(resolve_name)
            if not inner_names:
                logger.info("Skipping builtin type in resolution: %s", schema_name)
                continue
            if inner_names != [resolve_name]:
                for inner in inner_names:
                    if inner not in all_schemas:
                        queue.append((inner, import_source))
                continue

            file_path = resolve_type(
                resolve_name, import_source, ctags_index, project_root,
            )

            if file_path is None:
                if not quiet:
                    console.print(f"  [red]Could not resolve[/red] {schema_name}"
                                  f" (import: {import_source or 'none'})")
                _emit("resolving", name=schema_name, file=None)

                # Use inline schemas from endpoint descriptors if available
                inline_schema = inline_schemas.get(schema_name)
                if inline_schema:
                    all_schemas[schema_name] = inline_schema
                    if not quiet:
                        prop_count = len(inline_schema.get("properties", {}))
                        console.print(f"    [cyan]Built from inline schema ({prop_count} fields)[/cyan]")
                else:
                    # If the LLM already classified this as "unresolvable",
                    # ctags failing is expected — don't flag as x-unresolved.
                    # Only flag when the LLM expected resolution (import/class_to_file).
                    llm_res = llm_resolution_by_name.get(schema_name, "")
                    if llm_res == "unresolvable":
                        all_schemas[schema_name] = {
                            "type": "object",
                            "description": "Type marked unresolvable by extraction agent.",
                            "x-unresolved": True,
                        }
                    else:
                        all_schemas[schema_name] = {
                            "type": "object",
                            "description": "Schema could not be resolved from source code.",
                            "x-unresolved": True,
                        }
                continue

            file_key = str(file_path)
            if file_key in extracted_files or file_key in round_queued_files:
                # Track dotted names for deferred alias creation after Phase B
                if "." in schema_name or schema_name in qualified_context:
                    skipped_dotted.append(schema_name)
                else:
                    if not quiet:
                        console.print(f"  [dim]Already extracted {file_path.name}, skipping[/dim]")
                    _emit("already_extracted", file=file_path.name)
                continue

            if not quiet:
                console.print(f"  [bold]{schema_name}[/bold] → {file_path.relative_to(project_root)}")
            _emit("resolving", name=schema_name, file=str(file_path))

            file_text = file_path.read_text(encoding="utf-8", errors="replace")
            known_schemas = {
                n: schemas_snapshot[n] for n in schemas_snapshot
                if re.search(rf'\b{re.escape(n)}\b', file_text)
                and not schemas_snapshot[n].get("x-unresolved")
            }

            extraction_tasks.append((schema_name, file_path, known_schemas))
            round_queued_files.add(file_key)

        # Phase B: Run LLM extraction (parallelizable within the round)
        def _extract_one(
            schema_name: str, file_path: Path, known: dict[str, dict],
        ) -> tuple[str, Path, SchemaDescriptor | None, object | None, str | None]:
            ctx = SchemaExtractorContext(
                framework=framework,
                target_file=str(file_path),
                known_schemas=known,
            )
            try:
                descriptor, record = run_schema_extractor(
                    str(file_path), ctx, config=config,
                    telemetry=telemetry,
                )
                return (schema_name, file_path, descriptor, record, None)
            except Exception as e:
                return (schema_name, file_path, None, None, str(e))

        workers = config.max_workers_schema
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_extract_one, name, fpath, known): name
                for name, fpath, known in extraction_tasks
            }
            for future in as_completed(futures):
                schema_name, file_path, descriptor, record, error = future.result()
                if error:
                    if not quiet:
                        console.print(f"    [red]Extraction failed:[/red] {error}")
                    _emit("extract_failed", name=schema_name, error=error)
                    all_schemas[schema_name] = {
                        "type": "object",
                        "description": f"Schema extraction failed: {error}",
                        "x-unresolved": True,
                    }
                else:
                    if not quiet:
                        console.print(
                            f"    Extracted {record.schema_count} schema(s) in "
                            f"{record.duration_ms:.0f}ms"
                        )
                    schemas_dict = descriptor.to_json_schema_dict()
                    _emit("extracted", name=schema_name, file=str(file_path),
                          count=record.schema_count,
                          duration_ms=record.duration_ms,
                          schema_names=list(schemas_dict.keys()))
                    extracted_files.add(str(file_path))

                    if "." in schema_name:
                        leaf = schema_name.rsplit(".", 1)[1]
                        for extracted_name, extracted_schema in schemas_dict.items():
                            if extracted_name == leaf:
                                round_new_schemas[schema_name] = extracted_schema
                            else:
                                round_new_schemas[extracted_name] = extracted_schema
                    else:
                        round_new_schemas.update(schemas_dict)

                    if schema_name not in round_new_schemas:
                        lower_map = {
                            k.lower(): k for k in round_new_schemas
                        }
                        actual = lower_map.get(schema_name.lower())
                        if actual:
                            logger.info(
                                "Case alias: %s → %s (LLM used %s)",
                                schema_name, actual, actual,
                            )
                            round_new_schemas[schema_name] = round_new_schemas[actual]

        all_schemas.update(round_new_schemas)

        # Create aliases for dotted/qualified names that resolved to
        # already-extracted files.  The leaf schema exists in all_schemas
        # under its short name; we just need to add the alias.
        for dotted_name in skipped_dotted:
            if dotted_name in all_schemas:
                continue
            # Determine the leaf name to look up
            if dotted_name in qualified_context:
                original, _ = qualified_context[dotted_name]
                leaf = original.rsplit(".", 1)[1] if "." in original else original
            elif "." in dotted_name:
                leaf = dotted_name.rsplit(".", 1)[1]
            else:
                continue
            if leaf in all_schemas:
                all_schemas[dotted_name] = copy.deepcopy(all_schemas[leaf])
                if not quiet:
                    console.print(f"  [dim]Alias: {dotted_name} → {leaf}[/dim]")
                logger.info("Created alias: %s → %s (same file)", dotted_name, leaf)

        # Scan for new $ref targets
        raw_refs = scan_refs_in_schemas(round_new_schemas)
        new_refs: set[str] = set()
        for ref_name in raw_refs:
            for inner in _decompose_type_hint(ref_name):
                new_refs.add(inner)

        # Discover subtypes via ctags inheritance map
        for schema_name in list(round_new_schemas.keys()):
            children = inheritance_map.get(schema_name, [])
            for child in children:
                if child.name not in all_schemas and child.name not in new_refs:
                    new_refs.add(child.name)
                    if not quiet:
                        console.print(
                            f"  [magenta]Subtype discovered:[/magenta] "
                            f"{child.name} inherits {schema_name}"
                        )
                    _emit("subtype_discovered", child=child.name, parent=schema_name)

        unresolved = new_refs - set(all_schemas.keys())

        if unresolved:
            if not quiet:
                console.print(
                    f"\n  [cyan]New $refs discovered:[/cyan] {', '.join(sorted(unresolved))}"
                )
            _emit("new_refs", refs=list(unresolved))
            for ref_name in unresolved:
                queue.append((ref_name, None))
        else:
            if not quiet:
                console.print(f"\n  [green]No new unresolved $refs[/green]")
            _emit("no_new_refs")

    if queue:
        if not quiet:
            console.print(f"\n[yellow]Stopped after {max_depth} rounds. "
                           f"{len(queue)} refs still pending.[/yellow]")
        for name, _import_source in queue:
            if name not in all_schemas:
                all_schemas[name] = {
                    "type": "object",
                    "description": "Schema resolution exceeded max depth.",
                    "x-unresolved": True,
                }

    return all_schemas, inheritance_map, name_mapping


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

    if args.descriptor:
        if not os.path.isfile(args.descriptor):
            print(f"Error: descriptor not found: {args.descriptor}", file=sys.stderr)
            sys.exit(1)
        with open(args.descriptor) as f:
            data = json.load(f)
        desc_data = data.get("descriptor", data)
        descriptor = EndpointDescriptor.model_validate(desc_data)
        console.print(f"[dim]Loaded descriptor: {len(descriptor.endpoints)} endpoints[/dim]")
    else:
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

    all_schemas, _inheritance_map, _name_mapping = run_schema_loop(
        ref_hints=ref_hints,
        framework=args.framework,
        project_root=project_root,
        config=config,
        console=console,
        max_depth=args.max_depth,
    )

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
