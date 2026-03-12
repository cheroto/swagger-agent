"""Pipeline — top-level run_pipeline() stitching Scout → Route Extraction → Schema Loop → Assembly."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.rule import Rule

from swagger_agent.config import LLMConfig
from swagger_agent.models import (
    CompletenessChecklist,
    DiscoveryManifest,
    EndpointDescriptor,
)
from swagger_agent.agents.scout.harness import run_scout
from swagger_agent.agents.route_extractor.harness import (
    RouteExtractorContext,
    run_route_extractor,
)
from swagger_agent.infra.schema_loop import (
    collect_ref_hints_from_descriptor,
    run_schema_loop,
)
from swagger_agent.infra.assembler import AssemblyResult, assemble_spec
from swagger_agent.infra.validator import ValidationResult, validate_spec, check_completeness


@dataclass
class PipelineResult:
    yaml_str: str = ""
    spec: dict = field(default_factory=dict)
    manifest: DiscoveryManifest | None = None
    descriptors: list[EndpointDescriptor] = field(default_factory=list)
    schemas: dict[str, dict] = field(default_factory=dict)
    completeness: CompletenessChecklist = field(default_factory=CompletenessChecklist)
    validation: ValidationResult = field(default_factory=ValidationResult)
    failed_routes: list[tuple[str, str]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


def run_pipeline(
    target_dir: str,
    config: LLMConfig | None = None,
    console: Console | None = None,
    dashboard: object | None = None,
) -> PipelineResult:
    """Run the full pipeline: Scout → Route Extraction → Schema Loop → Assembly.

    Args:
        target_dir: Path to the target project directory.
        config: LLM configuration. Uses defaults if None.
        console: Rich console for output (defaults to stderr).
        dashboard: Optional PipelineDashboard for live display. When provided,
            console output is suppressed in favor of dashboard events.

    Returns:
        PipelineResult with the assembled spec and all intermediate artifacts.
    """
    console = console or Console(stderr=True)
    config = config or LLMConfig()
    result = PipelineResult()
    target_path = Path(target_dir).resolve()
    db = dashboard  # shorthand

    # ── Phase 1: Scout ──
    if db:
        db.phase_start(1, "Scout")
    else:
        console.print(Rule(" Phase 1: Scout ", style="bold blue"))
    t0 = time.monotonic()

    manifest, scout_record = run_scout(
        str(target_path), config=config,
        event_handler=db if db else None,
    )
    result.manifest = manifest
    result.timings["scout"] = (time.monotonic() - t0) * 1000

    scout_summary = (
        f"{manifest.framework}/{manifest.language}, "
        f"{len(manifest.route_files)} route(s), "
        f"{len(manifest.servers)} server(s)"
    )
    if db:
        db.phase_complete(1, scout_summary)
    else:
        console.print(f"[bold]Scout complete:[/bold] {scout_summary}")

    if not manifest.route_files:
        if not db:
            console.print("[yellow]No route files found. Returning empty spec.[/yellow]")
        assembly = assemble_spec(manifest, [], {})
        result.spec = assembly.spec
        result.yaml_str = assembly.yaml_str
        return result

    # ── Phase 2: Route Extraction ──
    if db:
        db.phase_start(2, "Route Extraction")
    else:
        console.print(Rule(" Phase 2: Route Extraction ", style="bold blue"))
    t0 = time.monotonic()
    descriptors: list[EndpointDescriptor] = []
    total_route_files = len(manifest.route_files)

    for idx, route_file in enumerate(manifest.route_files, 1):
        # Normalize: if LLM produced an absolute path, make it relative first
        if os.path.isabs(route_file):
            route_file = os.path.relpath(route_file, str(target_path))
        abs_path = str(target_path / route_file)

        if db:
            db.route_start(route_file, idx, total_route_files)
        else:
            console.print(f"  Extracting: [bold]{route_file}[/bold]")

        context = RouteExtractorContext(
            framework=manifest.framework,
            base_path=manifest.base_path,
            target_file=abs_path,
        )

        try:
            descriptor, record = run_route_extractor(abs_path, context, config=config)
            descriptors.append(descriptor)
            if db:
                db.route_complete(route_file, record.endpoint_count, record.duration_ms)
                db.route_endpoints_discovered(descriptor)
            else:
                console.print(
                    f"    {record.endpoint_count} endpoint(s) in {record.duration_ms:.0f}ms"
                )
        except Exception as e:
            if db:
                db.route_failed(route_file, str(e))
            else:
                console.print(f"    [red]Failed:[/red] {e}")
            result.failed_routes.append((route_file, str(e)))

    result.descriptors = descriptors
    result.timings["route_extraction"] = (time.monotonic() - t0) * 1000

    total_endpoints = sum(len(d.endpoints) for d in descriptors)
    routes_summary = (
        f"{len(descriptors)}/{total_route_files} files, "
        f"{total_endpoints} endpoint(s)"
    )
    if db:
        db.phase_complete(2, routes_summary)
    else:
        console.print(f"[bold]Routes complete:[/bold] {routes_summary}")
        if result.failed_routes:
            console.print(f"[yellow]{len(result.failed_routes)} file(s) failed[/yellow]")

    # ── Phase 3: Schema Resolution ──
    if db:
        db.phase_start(3, "Schema Resolution")
    else:
        console.print(Rule(" Phase 3: Schema Resolution ", style="bold blue"))
    t0 = time.monotonic()

    all_ref_hints: list[dict] = []
    for desc in descriptors:
        all_ref_hints.extend(collect_ref_hints_from_descriptor(desc))

    # Deduplicate by ref_hint name
    seen: set[str] = set()
    deduped_hints: list[dict] = []
    for hint in all_ref_hints:
        if hint["ref_hint"] not in seen:
            seen.add(hint["ref_hint"])
            deduped_hints.append(hint)

    if deduped_hints:
        if not db:
            console.print(
                f"  {len(deduped_hints)} unique ref(s): "
                f"{', '.join(h['ref_hint'] for h in deduped_hints)}"
            )
        schemas = run_schema_loop(
            ref_hints=deduped_hints,
            framework=manifest.framework,
            project_root=target_path,
            config=config,
            console=console,
            event_callback=db.schema_event if db else None,
        )
    else:
        if not db:
            console.print("[dim]No ref_hints to resolve.[/dim]")
        schemas = {}

    result.schemas = schemas
    result.timings["schema_resolution"] = (time.monotonic() - t0) * 1000

    resolved = sum(1 for s in schemas.values() if not s.get("x-unresolved"))
    unresolved = sum(1 for s in schemas.values() if s.get("x-unresolved"))
    schemas_summary = f"{resolved} resolved, {unresolved} unresolved"
    if db:
        db.phase_complete(3, schemas_summary)
    else:
        console.print(f"[bold]Schemas complete:[/bold] {schemas_summary}")

    # ── Phase 4: Assembly ──
    if db:
        db.phase_start(4, "Assembly")
    else:
        console.print(Rule(" Phase 4: Assembly ", style="bold blue"))
    t0 = time.monotonic()

    assembly = assemble_spec(manifest, descriptors, schemas)
    result.spec = assembly.spec
    result.yaml_str = assembly.yaml_str
    result.timings["assembly"] = (time.monotonic() - t0) * 1000

    path_count = len(result.spec.get("paths", {}))
    schema_count = len(result.spec.get("components", {}).get("schemas", {}))
    assembly_summary = f"{path_count} path(s), {schema_count} schema(s)"
    if db:
        db.assembly_complete(path_count, schema_count)
        db.phase_complete(4, assembly_summary)
    else:
        console.print(f"[bold]Assembled:[/bold] {assembly_summary}")

    # ── Phase 5: Validation ──
    if db:
        db.phase_start(5, "Validation")
    else:
        console.print(Rule(" Phase 5: Validation ", style="bold blue"))
    t0 = time.monotonic()

    result.validation = validate_spec(result.spec)
    result.completeness = check_completeness(result.spec, manifest, descriptors)
    result.timings["validation"] = (time.monotonic() - t0) * 1000

    err_count = len(result.validation.errors)
    warn_count = len(result.validation.warnings)
    if db:
        db.validation_complete(err_count, warn_count)
        if err_count:
            validation_summary = f"{err_count} error(s), {warn_count} warning(s)"
        elif warn_count:
            validation_summary = f"Clean, {warn_count} warning(s)"
        else:
            validation_summary = "Clean"
        db.phase_complete(5, validation_summary)
    else:
        if result.validation.errors:
            console.print(f"[red]Validation errors: {err_count}[/red]")
            for err in result.validation.errors:
                console.print(f"  [red]{err}[/red]")
        else:
            console.print("[green]No validation errors[/green]")

        if result.validation.warnings:
            console.print(f"[yellow]Warnings: {warn_count}[/yellow]")
            for warn in result.validation.warnings:
                console.print(f"  [yellow]{warn}[/yellow]")

    # Total time
    total_ms = sum(result.timings.values())
    result.timings["total"] = total_ms
    if not db:
        console.print(f"\n[dim]Total pipeline time: {total_ms / 1000:.1f}s[/dim]")

    return result
