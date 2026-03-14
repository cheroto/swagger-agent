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
import logging
import os
import re
import sys
from collections import deque

logger = logging.getLogger("swagger_agent.schema_loop")
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


# ── Type hint decomposition ──────────────────────────────────────────────
#
# LLMs emit ref_hints as raw type annotations from the source language:
#   List[User], Optional[str], Union[CreditCard, BankTransfer], Dict[str, Any]
#
# These must be decomposed into individual resolvable type names before
# queuing for schema resolution. The decomposition is language-agnostic —
# all languages use some form of Generic[T] or Generic<T> syntax.


# Types that map directly to JSON Schema primitives — never resolve these.
_BUILTIN_TYPES = frozenset({
    # Python
    "str", "int", "float", "bool", "bytes", "None", "NoneType",
    "dict", "list", "set", "tuple", "Any", "object",
    # Java / C# / TypeScript / Go / Rust
    "string", "String", "integer", "Integer", "long", "Long",
    "double", "Double", "number", "Number", "boolean", "Boolean",
    "void", "Void", "byte", "Byte", "char", "short",
    "Object", "Map", "HashMap", "Array", "List", "Set",
    "any", "unknown", "undefined", "null", "never",
})

# Wrappers that contain a single inner type (unwrap → resolve inner).
_PASSTHROUGH_WRAPPERS = frozenset({
    "List", "list", "Sequence", "Set", "set", "FrozenSet", "frozenset",
    "Tuple", "tuple", "Iterable", "Iterator", "Generator",
    "Optional", "Type", "ClassVar",
    "Array", "Vec", "vector", "IEnumerable", "IList", "ICollection",
    "Collection", "Deque", "deque", "Queue",
})

# Regex: Wrapper[InnerContent] or Wrapper<InnerContent>
_GENERIC_RE = re.compile(r"^(\w+)\s*[\[<](.+)[\]>]$")


def _decompose_type_hint(name: str) -> list[str]:
    """Decompose a type hint into individual resolvable type names.

    Returns a list of type names to queue for resolution. Skips builtins.
    Language-agnostic: handles Generic[T], Generic<T>, Union, Optional, etc.

    Examples:
        "User"                          → ["User"]
        "List[User]"                    → ["User"]
        "Optional[User]"                → ["User"]
        "Union[CreditCard, BankTransfer]" → ["CreditCard", "BankTransfer"]
        "Dict[str, Any]"                → []  (all builtins)
        "str"                           → []  (builtin)
        "dict[str, User]"               → ["User"]
    """
    name = name.strip()

    if name in _BUILTIN_TYPES:
        return []

    m = _GENERIC_RE.match(name)
    if not m:
        return [name]

    wrapper = m.group(1)
    inner_raw = m.group(2)

    # Union[A, B, C] or typing.Union → split on commas (respecting nesting)
    if wrapper in ("Union", "union"):
        parts = _split_generic_args(inner_raw)
        result = []
        for part in parts:
            result.extend(_decompose_type_hint(part))
        return result

    # Dict/Map[K, V] → only resolve V (values), skip K (keys are always primitives)
    if wrapper in ("Dict", "dict", "Map", "HashMap", "map",
                    "Mapping", "OrderedDict", "defaultdict"):
        parts = _split_generic_args(inner_raw)
        if len(parts) >= 2:
            return _decompose_type_hint(parts[-1])
        return []

    # Passthrough wrappers: List[X] → resolve X
    if wrapper in _PASSTHROUGH_WRAPPERS:
        parts = _split_generic_args(inner_raw)
        result = []
        for part in parts:
            result.extend(_decompose_type_hint(part))
        return result

    # Unknown wrapper — try to resolve the whole thing, but also try inner
    # e.g. IEnumerable<User> where IEnumerable isn't in our list
    parts = _split_generic_args(inner_raw)
    result = []
    for part in parts:
        result.extend(_decompose_type_hint(part))
    return result if result else [name]


def _split_generic_args(s: str) -> list[str]:
    """Split generic type arguments on commas, respecting nested brackets.

    "A, B, C"                     → ["A", "B", "C"]
    "str, List[int]"              → ["str", "List[int]"]
    "Dict[str, Any], User"        → ["Dict[str, Any]", "User"]
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in s:
        if ch in ("[", "<"):
            depth += 1
            current.append(ch)
        elif ch in ("]", ">"):
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    remainder = "".join(current).strip()
    if remainder:
        parts.append(remainder)
    return parts


def collect_ref_hints_from_descriptor(descriptor: EndpointDescriptor) -> list[dict]:
    """Extract all ref_hints from an endpoint descriptor.

    Sanitizes ref_hint values (strips stale $ref prefixes like
    '#/components/schemas/') before returning.

    All resolution types are included — even "unresolvable" hints are
    passed through so ctags/grep can attempt resolution. The LLM's
    classification is just a hint; infrastructure should always try.
    """
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
            clean_name = _sanitize_ref_hint(ref.ref_hint)
            if clean_name not in seen:
                seen.add(clean_name)
                hint = ref.model_dump(by_alias=True)
                hint["ref_hint"] = clean_name
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
) -> dict[str, dict]:
    """Run the schema extraction loop until all $refs are resolved.

    Args:
        ref_hints: Initial ref_hints from endpoint descriptors.
        framework: Framework name.
        project_root: Root of the target project.
        config: LLM config.
        console: Rich console for output (suppressed when event_callback is set).
        max_depth: Max recursion depth (safety net).
        event_callback: Optional callable(event, **kwargs) for structured events.
            When provided, console output for key events is suppressed.

    Returns:
        Tuple of (schemas dict, inheritance map).
        schemas: {"SchemaName": {JSON Schema}, ...}
        inheritance_map: {"ParentName": [CtagsEntry children], ...}
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

    all_schemas: dict[str, dict] = {}      # Accumulated schemas
    extracted_files: set[str] = set()       # Files already processed
    queue: deque[tuple[str, str | None]] = deque()

    # 1. Seed the queue from ref_hints (sanitize, decompose, skip builtins)
    for hint in ref_hints:
        raw_name = _sanitize_ref_hint(hint["ref_hint"])
        # Pick the best disambiguation hint: import_line > file_namespace > legacy import_source
        import_source = (
            hint.get("import_line")
            or hint.get("file_namespace")
            or hint.get("import_source")  # backward compat with old dict format
            or None
        )
        # Decompose Union/List/Optional/Dict wrappers, skip builtins
        inner_names = _decompose_type_hint(raw_name)
        if not inner_names and raw_name != raw_name:
            logger.info("Skipping builtin type hint: %s", raw_name)
        for name in inner_names:
            if name not in all_schemas:
                queue.append((name, import_source))

    depth = 0
    while queue and depth < max_depth:
        depth += 1
        batch_size = len(queue)
        if not quiet:
            console.print(Rule(f" Resolution Round {depth} ({batch_size} pending) ", style="bold yellow"))
        _emit("round_start", round=depth, pending=batch_size)

        # Process all items in the current batch
        round_new_schemas: dict[str, dict] = {}
        round_items = [queue.popleft() for _ in range(batch_size)]

        # Phase A: Resolve all type names to file paths (deterministic, fast)
        # Collect items that need LLM extraction
        extraction_tasks: list[tuple[str, Path, dict[str, dict]]] = []  # (name, path, known)
        round_queued_files: set[str] = set()  # Prevent duplicate extractions within same round

        # Snapshot all_schemas for building known_schemas context in this round
        schemas_snapshot = dict(all_schemas)

        for schema_name, import_source in round_items:
            if schema_name in all_schemas:
                continue

            # Decompose type hints that slipped through (e.g. from $ref scanning)
            inner_names = _decompose_type_hint(schema_name)
            if not inner_names:
                logger.info("Skipping builtin type in resolution: %s", schema_name)
                continue
            # If decomposition produced different names, re-queue them
            if inner_names != [schema_name]:
                for inner in inner_names:
                    if inner not in all_schemas:
                        queue.append((inner, import_source))
                continue

            file_path = resolve_type(
                schema_name, import_source, ctags_index, project_root,
            )

            if file_path is None:
                if not quiet:
                    console.print(f"  [red]Could not resolve[/red] {schema_name}"
                                  f" (import: {import_source or 'none'})")
                _emit("resolving", name=schema_name, file=None)
                all_schemas[schema_name] = {
                    "type": "object",
                    "description": "Schema could not be resolved from source code.",
                    "x-unresolved": True,
                }
                continue

            file_key = str(file_path)
            if file_key in extracted_files or file_key in round_queued_files:
                if not quiet:
                    console.print(f"  [dim]Already extracted {file_path.name}, skipping[/dim]")
                _emit("already_extracted", file=file_path.name)
                continue

            if not quiet:
                console.print(f"  [bold]{schema_name}[/bold] → {file_path.relative_to(project_root)}")
            _emit("resolving", name=schema_name, file=str(file_path))

            # Build known_schemas context from the round snapshot
            file_text = file_path.read_text(encoding="utf-8", errors="replace")
            known_schemas = {
                n: schemas_snapshot[n] for n in schemas_snapshot
                if n in file_text and not schemas_snapshot[n].get("x-unresolved")
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
                    _emit("extracted", name=schema_name, file=str(file_path),
                          count=record.schema_count,
                          duration_ms=record.duration_ms,
                          schema_names=list(descriptor.schemas.keys()))
                    extracted_files.add(str(file_path))

                    # When a dotted name was requested (e.g. "Create.Command"),
                    # store the matching leaf schema under the dotted name instead
                    # of the bare name. This prevents name collisions when multiple
                    # files define types with the same leaf name (e.g. Command).
                    # Other schemas from the file keep their bare names.
                    if "." in schema_name:
                        leaf = schema_name.rsplit(".", 1)[1]
                        for extracted_name, extracted_schema in descriptor.schemas.items():
                            if extracted_name == leaf:
                                # Store under dotted name (the canonical ref_hint)
                                round_new_schemas[schema_name] = extracted_schema
                            else:
                                # Other types from same file — keep bare name
                                round_new_schemas[extracted_name] = extracted_schema
                    else:
                        round_new_schemas.update(descriptor.schemas)

                    # Case-insensitive alias: if the requested schema_name
                    # doesn't match any key in the LLM output (common with
                    # small LLMs that change casing), find the match and
                    # alias under the canonical ref_hint name. This ensures
                    # $refs built from ref_hints find their schemas.
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

        # Merge new schemas
        all_schemas.update(round_new_schemas)

        # Scan for new $ref targets not yet resolved
        raw_refs = scan_refs_in_schemas(round_new_schemas)
        # Decompose any wrapped types found in $refs
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

    return all_schemas, inheritance_map


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
    all_schemas, _inheritance_map = run_schema_loop(
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
