"""Pipeline — top-level run_pipeline() stitching Scout → Route Extraction → Schema Loop → Assembly."""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("swagger_agent.pipeline")

from rich.console import Console
from rich.rule import Rule

from swagger_agent.config import LLMConfig
from swagger_agent.models import (
    CompletenessChecklist,
    DiscoveryManifest,
    EndpointDescriptor,
)
from swagger_agent.agents.scout.harness import run_scout
from swagger_agent.infra.prescan import run_prescan
from swagger_agent.infra.detectors.result import PrescanResult
from swagger_agent.agents.route_extractor.harness import (
    RouteExtractorContext,
    RouteExtractorRunRecord,
    run_route_extractor,
)
from swagger_agent.infra.schema_loop import (
    collect_ref_hints_from_descriptor,
    run_schema_loop,
)
from swagger_agent.infra.assembler import AssemblyResult, assemble_spec
from swagger_agent.infra.validator import ValidationResult, validate_spec, check_completeness
from swagger_agent.telemetry import Telemetry


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
    telemetry: Telemetry = field(default_factory=Telemetry)


def _prescan_to_manifest(prescan: PrescanResult, target_dir: str) -> DiscoveryManifest:
    """Convert a PrescanResult directly into a DiscoveryManifest (no LLM)."""
    return DiscoveryManifest(
        framework=prescan.framework or "unknown",
        language=prescan.language or "unknown",
        route_files=prescan.route_files,
        servers=prescan.servers,
        base_path=prescan.base_path,
        default_auth_mode=prescan.auth_mode or "",
        default_auth_hint=prescan.auth_context_hint,
    )


def run_pipeline(
    target_dir: str,
    config: LLMConfig | None = None,
    console: Console | None = None,
    dashboard: object | None = None,
    skip_scout: bool = False,
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

    # ── Phase 0: Pre-scan (deterministic) ──
    if db:
        db.phase_start(0, "Pre-scan")
    else:
        console.print(Rule(" Phase 0: Pre-scan ", style="bold blue"))
    t0 = time.monotonic()

    prescan_result = run_prescan(str(target_path))
    result.timings["prescan"] = (time.monotonic() - t0) * 1000

    prescan_summary = (
        f"{prescan_result.framework or '?'}/{prescan_result.language or '?'}, "
        f"{len(prescan_result.route_files)} tentative route(s)"
    )
    if db:
        db.phase_complete(0, prescan_summary)
    else:
        console.print(f"[bold]Pre-scan:[/bold] {prescan_summary}")

    # ── Phase 1: Scout (or skip with prescan) ──
    if skip_scout:
        phase1_label = "Scout [bold yellow](skipped — using prescan)[/bold yellow]"
        if db:
            db.phase_start(1, "Scout (skipped)")
        else:
            console.print(Rule(" Phase 1: Scout (skipped — prescan only) ", style="bold yellow"))

        manifest = _prescan_to_manifest(prescan_result, str(target_path))
        result.manifest = manifest
        result.timings["scout"] = 0.0

        scout_summary = (
            f"{manifest.framework}/{manifest.language}, "
            f"{len(manifest.route_files)} route(s), "
            f"{len(manifest.servers)} server(s) [prescan-only]"
        )
        if db:
            db.phase_complete(1, scout_summary)
        else:
            console.print(f"[bold]Prescan manifest:[/bold] {scout_summary}")
    else:
        if db:
            db.phase_start(1, "Scout")
        else:
            console.print(Rule(" Phase 1: Scout ", style="bold blue"))
        t0 = time.monotonic()

        manifest, scout_record = run_scout(
            str(target_path), config=config,
            event_handler=db if db else None,
            prescan=prescan_result,
            telemetry=result.telemetry,
        )
        # Propagate auth context from prescan (Scout doesn't discover auth)
        if prescan_result.auth_context_hint and not manifest.default_auth_hint:
            manifest.default_auth_mode = prescan_result.auth_mode or ""
            manifest.default_auth_hint = prescan_result.auth_context_hint
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

    # --- Round 1: Extract all route files (Phase 1 + Phase 2) ---
    # After Round 1, collect mount_maps from Phase 1 results.
    # If any mount_maps resolve to other route files, re-run Phase 2
    # on those files with the mount_prefix injected.

    def _resolve_mount_maps(
        records: dict[str, RouteExtractorRunRecord],
        route_files: list[str],
    ) -> dict[str, str]:
        """Resolve mount_map entries from all run records to route file paths.

        For each file that reported a mount_map in Phase 1, fuzzy-match
        the keys to other files in the route_files list.
        """
        import re as _re
        prefixes: dict[str, str] = {}

        def _tokenize(s: str) -> set[str]:
            tokens = _re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]|\d+", s)
            return {t.lower() for t in tokens}

        _NOISE = {"register", "routes", "route", "router", "controller", "handler"}

        for source_file, record in records.items():
            if not record.mount_map:
                continue
            for key, prefix in record.mount_map.items():
                key_tokens = _tokenize(key) - _NOISE
                key_norm = key.lower().replace("_", "").replace("-", "").replace(".", "")

                best_match = None
                best_score = 0
                for rf in route_files:
                    if rf == source_file:
                        continue
                    stem = os.path.splitext(os.path.basename(rf))[0]
                    stem_tokens = _tokenize(stem) - _NOISE
                    stem_norm = stem.lower().replace("_", "").replace("-", "").replace(".", "")

                    if key_norm == stem_norm:
                        best_match, best_score = rf, 100
                        break

                    score = 0
                    if key_tokens and stem_tokens:
                        overlap = len(key_tokens & stem_tokens)
                        if overlap:
                            score = overlap * 10

                    prefix_stem = prefix.strip("/").split("/")[-1].lower()
                    if prefix_stem and prefix_stem in stem_norm:
                        score = max(score, len(prefix_stem) + 5)

                    if score > best_score:
                        best_score = score
                        best_match = rf

                # If no match in route_files, search the same directory on disk
                if not best_match or best_score < 5:
                    source_dir = os.path.dirname(source_file)
                    abs_source_dir = str(target_path / source_dir) if source_dir else str(target_path)
                    if os.path.isdir(abs_source_dir):
                        for fname in os.listdir(abs_source_dir):
                            fpath = os.path.join(source_dir, fname) if source_dir else fname
                            if fpath == source_file or fpath in prefixes:
                                continue
                            fstem = os.path.splitext(fname)[0]
                            fstem_norm = fstem.lower().replace("_", "").replace("-", "").replace(".", "")
                            if key_norm == fstem_norm:
                                best_match = fpath
                                best_score = 100
                                # Add to route_files so it gets extracted in Round 1.5
                                if fpath not in route_files:
                                    route_files.append(fpath)
                                break

                if best_match and best_match not in prefixes:
                    prefixes[best_match] = prefix
        return prefixes

    def _extract_one_route(
        idx: int, route_file: str, mount_prefix: str = "",
    ) -> tuple[int, str, EndpointDescriptor | None, RouteExtractorRunRecord | None, str | None]:
        """Worker: extract endpoints from a single route file."""
        if os.path.isabs(route_file):
            route_file = os.path.relpath(route_file, str(target_path))
        abs_path = str(target_path / route_file)

        if db:
            db.route_start(route_file, idx, total_route_files)
        else:
            prefix_note = f" (mount: {mount_prefix})" if mount_prefix else ""
            console.print(f"  Extracting: [bold]{route_file}[/bold]{prefix_note}")

        ctx = RouteExtractorContext(
            framework=manifest.framework,
            base_path=manifest.base_path,
            target_file=abs_path,
            mount_prefix=mount_prefix,
            default_auth_mode=manifest.default_auth_mode,
            default_auth_hint=manifest.default_auth_hint,
        )
        try:
            descriptor, record = run_route_extractor(abs_path, ctx, config=config, telemetry=result.telemetry)
            return (idx, route_file, descriptor, record, None)
        except Exception as e:
            return (idx, route_file, None, None, str(e))

    # Round 1: extract all files without mount prefixes
    workers = config.max_workers_route
    records_by_file: dict[str, RouteExtractorRunRecord] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_extract_one_route, idx, rf): idx
            for idx, rf in enumerate(manifest.route_files, 1)
        }
        results_by_idx: dict[int, tuple] = {}
        for future in as_completed(futures):
            idx, route_file, descriptor, record, error = future.result()
            results_by_idx[idx] = (route_file, descriptor, record, error)
            if record:
                records_by_file[route_file] = record

            if error:
                if db:
                    db.route_failed(route_file, error)
                else:
                    console.print(f"    [red]Failed:[/red] {error}")
            else:
                if db:
                    db.route_complete(route_file, record.endpoint_count, record.duration_ms)
                    db.route_endpoints_discovered(descriptor)
                else:
                    console.print(
                        f"    {record.endpoint_count} endpoint(s) in {record.duration_ms:.0f}ms"
                    )

    # Resolve mount prefixes from Phase 1 mount_maps
    mount_prefixes = _resolve_mount_maps(records_by_file, manifest.route_files)

    # Round 2: re-extract files that got a mount prefix.
    # REPLACES Round 1 results for these files (not append).
    if mount_prefixes:
        if not db:
            console.print(f"  Re-extracting {len(mount_prefixes)} file(s) with mount prefixes: "
                          f"{mount_prefixes}")
        next_idx = max(results_by_idx.keys(), default=0) + 1
        for rf, prefix in mount_prefixes.items():
            idx_for_rf = next(
                (idx for idx, (f, *_) in results_by_idx.items() if f == rf), None
            )
            if idx_for_rf is None:
                # Newly discovered file from mount_map — extract fresh with prefix
                if not db:
                    console.print(f"  Extracting (from mount_map): [bold]{rf}[/bold] (mount: {prefix})")
                _, route_file, descriptor, record, error = _extract_one_route(
                    next_idx, rf, mount_prefix=prefix,
                )
                results_by_idx[next_idx] = (route_file, descriptor, record, error)
                next_idx += 1
                if error:
                    if not db:
                        console.print(f"    [red]Failed:[/red] {error}")
                elif not db:
                    console.print(f"    {record.endpoint_count} endpoint(s) with prefix {prefix}")
                continue
            _, route_file, descriptor, record, error = _extract_one_route(
                idx_for_rf, rf, mount_prefix=prefix,
            )
            if not error:
                # Replace Round 1 result — mount-prefixed extraction supersedes
                results_by_idx[idx_for_rf] = (route_file, descriptor, record, error)
                if not db:
                    console.print(f"    {record.endpoint_count} endpoint(s) with prefix {prefix}")
            else:
                # Keep Round 1 result on failure
                if not db:
                    console.print(f"    [red]Re-extract failed (keeping Round 1):[/red] {error}")

    # Identify pure registry files: files that produced mount_maps where ALL
    # mapped sub-files were successfully re-extracted. These files' Round 1
    # endpoints are noise (they read controller code inline, not their own routes).
    registry_files: set[str] = set()
    if mount_prefixes:
        for source_file, record in records_by_file.items():
            if not record.mount_map:
                continue
            # Check if all mount targets were re-extracted
            mapped_count = sum(1 for rf in mount_prefixes if rf != source_file)
            if mapped_count > 0:
                registry_files.add(source_file)

    # Append in original file order for deterministic spec output
    for idx in sorted(results_by_idx):
        route_file, descriptor, record, error = results_by_idx[idx]
        if error:
            result.failed_routes.append((route_file, error))
        elif route_file in registry_files:
            # Skip registry files — their endpoints came from reading sub-router code
            if not db:
                console.print(
                    f"  [dim]Skipping registry file {route_file} "
                    f"({len(descriptor.endpoints)} endpoints from inline reads)[/dim]"
                )
        else:
            descriptors.append(descriptor)

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

    # Deduplicate by (name, import_line, file_namespace) so that the same
    # type name from different import contexts survives as separate hints.
    seen: set[tuple[str, str, str]] = set()
    deduped_hints: list[dict] = []
    for hint in all_ref_hints:
        key = (hint["ref_hint"], hint.get("import_line", ""), hint.get("file_namespace", ""))
        if key not in seen:
            seen.add(key)
            deduped_hints.append(hint)

    inheritance_map: dict = {}
    name_mapping: dict[tuple[str, str], str] = {}
    if deduped_hints:
        if not db:
            console.print(
                f"  {len(deduped_hints)} unique ref(s): "
                f"{', '.join(h['ref_hint'] for h in deduped_hints)}"
            )
        # Collect inline schemas from all descriptors
        all_inline_schemas: dict[str, dict] = {}
        for desc in descriptors:
            all_inline_schemas.update(desc.inline_schemas_as_dict())

        schemas, inheritance_map, name_mapping = run_schema_loop(
            ref_hints=deduped_hints,
            framework=manifest.framework,
            project_root=target_path,
            config=config,
            console=console,
            event_callback=db.schema_event if db else None,
            telemetry=result.telemetry,
            inline_schemas=all_inline_schemas or None,
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

    assembly = assemble_spec(
        manifest, descriptors, schemas,
        inheritance_map=inheritance_map,
        name_mapping=name_mapping,
    )
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
