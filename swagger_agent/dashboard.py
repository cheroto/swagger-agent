"""Unified pipeline dashboard — animated mascot + phase progress + activity log.

A single Rich Live display that shows progress across all agents and pipeline
phases. Integrates the ASCII mascot which evolves as the pipeline progresses.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from swagger_agent.agents.scout.harness import ScoutEventHandler, StateUpdates
from swagger_agent.models import CompletenessChecklist, DiscoveryManifest


# ── Mascot Data (self-contained to avoid root-level import) ──────────────────

_COLORS = {
    "{B}": "\033[94m",
    "{Y}": "\033[93m",
    "{W}": "\033[97m",
    "{C}": "\033[96m",
    "{R}": "\033[91m",
    "{G}": "\033[92m",
    "{M}": "\033[95m",
    "{X}": "\033[0m",
}

_TEMPLATES = {
    1: [
        "                            ",
        "                            ",
        "        {Y}/\\{B}        {Y}/\\{B}        ",
        "       {Y}/{B}  \\______/  {Y}\\{B}       ",
        "      |   {W}.------.{B}   |      ",
        "      |  {W}|{B}{EYE}{W}|{B}  |      ",
        "      |   {W}'------'{B}   |      ",
        "      |  {R}.--------.{B}  |      ",
        "     /|  {R}\\________/{B}  |\\     ",
        "    |_|              |_|    ",
        "      |______________|      ",
        "        | |      | |        ",
        "        |_|      |_|        ",
    ],
    2: [
        "                            ",
        "                            ",
        "        {Y}/\\{B}  {G}____{B}  {Y}/\\{B}        ",
        "       {Y}/{B}  \\{G}======{B}/  {Y}\\{B}       ",
        "      |   {W}.------.{B}   |      ",
        "      |  {W}|{B}{EYE}{W}|{B}  |      ",
        "      |   {W}'------'{B}   |      ",
        "      | {G}[==========]{B} |      ",
        "     /| {G}[==========]{B} |\\     ",
        "    {G}[_]{B} {G}[==========]{B} {G}[_]{B}    ",
        "      |______________|      ",
        "        {G}| |{B}      {G}| |{B}        ",
        "        {G}[_]{B}      {G}[_]{B}        ",
    ],
    3: [
        "                            ",
        "            {M}____{B}            ",
        "        {Y}/\\{B} {M}/_{Y}**{M}_\\{B} {Y}/\\{B}        ",
        "  {C}*{M}O{C}*{B}  {Y}/{B}  \\{M}/____\\{B}/  {Y}\\{B}       ",
        "   {M}|{B}  |   {M}[------]{B}   |      ",
        "  {M}-+-{B} |  {M}|{B}{EYE}{M}|{B}  |      ",
        "   {M}|{B}  |   {M}[------]{B}   |      ",
        "   {M}|{B}  | {M}~::::::::::~{B} |      ",
        "   {M}|{B} /| {M}~::::::::::~{B} |\\     ",
        "   {M}|{B}{M}[_]{B} {M}~::::::::::~{B} {M}[_]{B}    ",
        "  {M}/ \\{B} |______________|      ",
        "    {C}~{B}   {M}| |{B}      {M}| |{B}   {C}~{B}    ",
        "        {M}[_]{B}      {M}[_]{B}        ",
    ],
}

_EYES = {
    1: {
        "idle": "  {C}[+]{B}   ",
        "look_left": " {C}[+]{B}    ",
        "look_right": "    {C}[+]{B} ",
        "blink": " {C}>----<{B} ",
    },
    2: {
        "idle": "  {C}[+]{B}   ",
        "look_left": " {C}[+]{B}    ",
        "look_right": "    {C}[+]{B} ",
        "blink": " {C}>----<{B} ",
    },
    3: {
        "idle": "  {C}[*]{B}   ",
        "look_left": " {C}[*]{B}    ",
        "look_right": "    {C}[*]{B} ",
        "blink": " {M}>----<{B} ",
    },
}

_ANIM_SEQUENCE = [
    "idle", "idle", "idle", "blink", "idle",
    "look_left", "look_left", "look_right", "look_right", "idle",
]

# Pre-process all animation frames
_FRAMES: dict[int, dict[str, str]] = {}


def _preprocess():
    for lvl in (1, 2, 3):
        _FRAMES[lvl] = {}
        for anim in ("idle", "look_left", "look_right", "blink"):
            lines = []
            for line in _TEMPLATES[lvl]:
                fmt = "{B}" + line.replace("{EYE}", _EYES[lvl][anim]) + "{X}"
                for tag, code in _COLORS.items():
                    fmt = fmt.replace(tag, code)
                lines.append(fmt)
            _FRAMES[lvl][anim] = "\n".join(lines)


_preprocess()


# ── Phase definitions ────────────────────────────────────────────────────────

PHASES = [
    (0, "Pre-scan"),
    (1, "Scout"),
    (2, "Route Extraction"),
    (3, "Schema Resolution"),
    (4, "Assembly"),
    (5, "Spec Cleanup"),
    (6, "Validation"),
]

_LEVEL_FOR_PHASE = {0: 1, 1: 1, 2: 2, 3: 3, 4: 3, 5: 3, 6: 3}  # prescan+scout=1, routes=2, schemas+=3
_LEVEL_NAMES = {1: "Scout", 2: "Extractor", 3: "Architect"}


# ── Dashboard ────────────────────────────────────────────────────────────────


class PipelineDashboard(ScoutEventHandler):
    """Unified live dashboard for the full Swagger Agent pipeline.

    Implements ScoutEventHandler so it can receive Scout lifecycle events
    directly. For other phases, the pipeline calls methods explicitly.
    """

    def __init__(self, console: Console | None = None):
        self.console = console or Console(stderr=True)
        self._live: Live | None = None
        self._start_time: float = 0.0
        self._lock = threading.Lock()

        # Mascot
        self._mascot_level = 1
        self._frame_idx = 0

        # Phase tracking
        self._current_phase: int | None = None
        self._phase_status: dict[int, str] = {}  # "pending"|"active"|"complete"
        self._phase_summary: dict[int, str] = {}

        # Scout state (for detail panel)
        self._scout_turn = 0
        self._scout_remaining: list[str] = []
        self._scout_completed: list[str] = []
        self._scout_findings: dict[str, str] = {}

        # Route extraction state
        self._routes_in_flight: set[str] = set()
        self._route_total = 0
        self._route_endpoints_total = 0
        self._route_files_done = 0

        # Schema state
        self._schema_round = 0
        self._schema_pending = 0
        self._schemas_in_flight: set[str] = set()
        self._schema_resolved = 0
        self._schema_unresolved = 0

        # Assembly / validation
        self._assembly_paths = 0
        self._assembly_schemas = 0
        self._validation_errors = 0
        self._validation_warnings = 0

        # ── Spec artifacts (accumulated, shown in the "Spec Data" panel) ──
        self._spec_framework = ""
        self._spec_language = ""
        self._spec_servers: list[str] = []
        self._spec_base_path = ""
        self._spec_route_files: list[str] = []
        self._spec_endpoints: list[tuple[str, str, str | None]] = []  # (method, path, security)
        self._spec_security_schemes: set[str] = set()
        self._spec_schema_names: list[str] = []  # resolved schema names in order
        self._spec_unresolved_schemas: list[str] = []

        # Issue counters (lightweight — just counts)
        self._failed_routes: int = 0
        self._failed_schemas: int = 0

        # Activity log
        self._logs: list[str] = []
        self._max_logs = 12

        # Completeness (replaces activity log when run finishes)
        self._completeness: CompletenessChecklist | None = None
        self._finished = False

        # Animation
        self._running = False
        self._anim_thread: threading.Thread | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        self._start_time = time.monotonic()
        self._running = True
        for num, _ in PHASES:
            self._phase_status[num] = "pending"

        self._live = Live(
            self._build_layout(),
            console=self.console,
            refresh_per_second=4,
            transient=False,
        )
        self._live.start()

        self._anim_thread = threading.Thread(target=self._animate, daemon=True)
        self._anim_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._live:
            self._refresh()  # Final update
            self._live.stop()
            self._live = None

    def _animate(self) -> None:
        while self._running:
            self._frame_idx += 1
            self._refresh()
            time.sleep(0.3)

    def _refresh(self) -> None:
        if self._live:
            try:
                self._live.update(self._build_layout())
            except Exception:
                pass

    def _elapsed(self) -> float:
        return time.monotonic() - self._start_time

    def _log(self, tag: str, msg: str, style: str = "dim") -> None:
        elapsed = self._elapsed()
        entry = f"[dim]{elapsed:6.1f}s[/dim]  [{style}]{tag:8s}[/{style}]  {msg}"
        with self._lock:
            self._logs.append(entry)
            if len(self._logs) > 50:
                self._logs = self._logs[-50:]

    # ── Phase lifecycle (called by pipeline.py) ───────────────────────────

    def phase_start(self, phase: int, name: str) -> None:
        self._current_phase = phase
        self._phase_status[phase] = "active"
        self._mascot_level = _LEVEL_FOR_PHASE.get(phase, 1)
        self._log("PHASE", f"[bold]Starting {name}[/bold]", "bold blue")
        self._refresh()

    def phase_complete(self, phase: int, summary: str) -> None:
        self._phase_status[phase] = "complete"
        self._phase_summary[phase] = summary
        name = dict(PHASES)[phase]
        self._log("DONE", f"[green]✓ {name}[/green]: {summary}", "green")
        self._refresh()

    # ── Route extraction events ───────────────────────────────────────────

    def route_start(self, file: str, index: int, total: int) -> None:
        with self._lock:
            self._routes_in_flight.add(file)
            self._route_total = total
        short = file.rsplit("/", 1)[-1]
        self._log("ROUTE", f"Extracting [bold]{short}[/bold] ({index}/{total})", "yellow")
        self._refresh()

    def route_complete(self, file: str, endpoints: int, duration_ms: float) -> None:
        with self._lock:
            self._route_endpoints_total += endpoints
            self._route_files_done += 1
            self._routes_in_flight.discard(file)
        short = file.rsplit("/", 1)[-1]
        self._log("ROUTE", f"[green]✓[/green] {short} → {endpoints} endpoint(s) ({duration_ms:.0f}ms)", "green")
        self._refresh()

    def route_endpoints_discovered(self, descriptor) -> None:
        """Feed extracted endpoint details into the spec artifacts panel.

        Called by pipeline after each successful route extraction.
        ``descriptor`` is an EndpointDescriptor pydantic model.
        """
        with self._lock:
            for ep in descriptor.endpoints:
                sec = ", ".join(s.name for s in ep.security) if ep.security else None
                self._spec_endpoints.append((ep.method, ep.path, sec))
                if ep.security:
                    for s in ep.security:
                        self._spec_security_schemes.add(s.name)
        self._refresh()

    def route_failed(self, file: str, error: str) -> None:
        with self._lock:
            self._failed_routes += 1
            self._routes_in_flight.discard(file)
        short = file.rsplit("/", 1)[-1]
        self._log("ROUTE", f"[red]✗[/red] {short}: {error}", "red")
        self._refresh()

    # ── Schema loop events (callback for run_schema_loop) ─────────────────

    def schema_event(self, event: str, **kwargs) -> None:
        if event == "ctags_built":
            self._log("SCHEMA", f"Indexed {kwargs.get('count', 0)} type definitions", "dim")
        elif event == "round_start":
            with self._lock:
                self._schema_round = kwargs.get("round", 0)
                self._schema_pending = kwargs.get("pending", 0)
            self._log("SCHEMA", f"Round {self._schema_round} ({self._schema_pending} pending)", "yellow")
        elif event == "already_extracted":
            self._log("SCHEMA", f"[dim]{kwargs.get('file', '?')} already extracted[/dim]", "dim")
        elif event == "resolving":
            name = kwargs.get("name", "?")
            file = kwargs.get("file")
            with self._lock:
                self._schemas_in_flight.add(name)
            if file:
                short = str(file).rsplit("/", 1)[-1]
                self._log("SCHEMA", f"{name} → [bold]{short}[/bold]", "cyan")
            else:
                self._log("SCHEMA", f"[red]Could not resolve[/red] {name}", "red")
                with self._lock:
                    self._schema_unresolved += 1
                    self._schemas_in_flight.discard(name)
                    if name not in self._spec_unresolved_schemas:
                        self._spec_unresolved_schemas.append(name)
        elif event == "extracted":
            count = kwargs.get("count", 0)
            duration = kwargs.get("duration_ms", 0)
            name = kwargs.get("name", "")
            file = kwargs.get("file", "")
            short = str(file).rsplit("/", 1)[-1]
            with self._lock:
                self._schema_resolved += count
                self._schemas_in_flight.discard(name)
                schema_names = kwargs.get("schema_names", [])
                for sn in schema_names:
                    if sn not in self._spec_schema_names:
                        self._spec_schema_names.append(sn)
            self._log("SCHEMA", f"[green]✓[/green] {short} → {count} schema(s) ({duration:.0f}ms)", "green")
        elif event == "extract_failed":
            name = kwargs.get("name", "?")
            with self._lock:
                self._schema_unresolved += 1
                self._failed_schemas += 1
                self._schemas_in_flight.discard(name)
                if name not in self._spec_unresolved_schemas:
                    self._spec_unresolved_schemas.append(name)
            self._log("SCHEMA", f"[red]✗[/red] {name}: {kwargs.get('error', '')}", "red")
        elif event == "new_refs":
            refs = kwargs.get("refs", [])
            self._log("SCHEMA", f"New $refs: {', '.join(sorted(refs))}", "cyan")
        elif event == "no_new_refs":
            self._log("SCHEMA", "[green]No new unresolved $refs[/green]", "green")
        self._refresh()

    # ── Assembly / Validation events ──────────────────────────────────────

    def assembly_complete(self, paths: int, schemas: int) -> None:
        self._assembly_paths = paths
        self._assembly_schemas = schemas
        self._refresh()

    def validation_complete(self, errors: int, warnings: int) -> None:
        self._validation_errors = errors
        self._validation_warnings = warnings
        if errors:
            self._log("VALID", f"[red]{errors} error(s)[/red], {warnings} warning(s)", "red")
        elif warnings:
            self._log("VALID", f"[green]No errors[/green], {warnings} warning(s)", "yellow")
        else:
            self._log("VALID", "[green]Clean — no errors or warnings[/green]", "green")
        self._refresh()

    def set_completeness(self, completeness: CompletenessChecklist) -> None:
        """Signal that the pipeline has finished and show completeness in the log area."""
        self._completeness = completeness
        self._finished = True
        self._refresh()

    # ── ScoutEventHandler implementation ──────────────────────────────────

    def on_turn_start(self, turn: int, remaining_tasks: list[str]) -> None:
        self._scout_turn = turn
        all_tasks = ["identify_framework", "find_route_files", "find_servers"]
        self._scout_remaining = list(remaining_tasks)
        self._scout_completed = [t for t in all_tasks if t not in remaining_tasks]
        self._refresh()

    def on_scratchpad_update(self, turn: int, scratchpad: str) -> None:
        pass  # We don't display scratchpad in the unified dashboard

    def on_state_update(self, turn: int, updates: StateUpdates, remaining_tasks: list[str]) -> None:
        all_tasks = ["identify_framework", "find_route_files", "find_servers"]
        self._scout_remaining = list(remaining_tasks)
        self._scout_completed = [t for t in all_tasks if t not in remaining_tasks]

        updates_dict = updates.model_dump(exclude_none=True)
        if "framework" in updates_dict:
            self._scout_findings["Framework"] = updates_dict["framework"]
            self._spec_framework = updates_dict["framework"]
        if "language" in updates_dict:
            self._scout_findings["Language"] = updates_dict["language"]
            self._spec_language = updates_dict["language"]
        if "route_files" in updates_dict:
            for rf in updates_dict["route_files"]:
                if rf not in self._spec_route_files:
                    self._spec_route_files.append(rf)
            self._scout_findings["Routes"] = f"{len(self._spec_route_files)} file(s)"
        if "servers" in updates_dict:
            servers = updates_dict["servers"]
            self._scout_findings["Servers"] = ", ".join(servers[:2])
            if len(servers) > 2:
                self._scout_findings["Servers"] += f" +{len(servers) - 2}"
            self._spec_servers = list(servers)
        if "base_path" in updates_dict:
            self._spec_base_path = updates_dict["base_path"]

        completed = updates.completed_tasks
        if completed:
            for t in completed:
                self._log("SCOUT", f"[green]✓[/green] Task complete: {t}", "green")
        self._refresh()

    def on_tool_execute(self, turn: int, tool: str, args: dict) -> None:
        args_short = json.dumps(args, default=str)
        if len(args_short) > 45:
            args_short = args_short[:42] + "..."
        self._log("SCOUT", f"{tool}({args_short})", "blue")
        self._refresh()

    def on_tool_result(self, turn: int, tool: str, summary: str) -> None:
        self._log("SCOUT", f"  → {summary}", "dim")
        self._refresh()

    def on_tool_error(self, turn: int, tool: str, error: str) -> None:
        self._log("SCOUT", f"[red]✗ {tool}:[/red] {error}", "red")
        self._refresh()

    def on_manifest(self, manifest: DiscoveryManifest) -> None:
        self._scout_findings["Framework"] = manifest.framework
        self._scout_findings["Language"] = manifest.language
        self._scout_findings["Routes"] = f"{len(manifest.route_files)} file(s)"
        servers = manifest.servers
        self._scout_findings["Servers"] = ", ".join(servers[:2]) if servers else "-"

        # Populate spec artifacts
        self._spec_framework = manifest.framework
        self._spec_language = manifest.language
        self._spec_servers = list(manifest.servers)
        self._spec_base_path = manifest.base_path
        self._spec_route_files = list(manifest.route_files)
        self._refresh()

    def on_max_turns(self, turn: int) -> None:
        self._log("SCOUT", f"[yellow]Max turns reached ({turn})[/yellow]", "yellow")
        self._refresh()

    def on_llm_error(self, turn: int, error: Exception, retries_left: int) -> None:
        self._log("SCOUT", f"[red]LLM error[/red] ({retries_left} retries): {error}", "red")
        self._refresh()

    # ── Layout builders ───────────────────────────────────────────────────

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="log", size=min(self._max_logs + 2, 14)),
        )
        layout["body"].split_row(
            Layout(name="left", size=32),
            Layout(name="right", ratio=1),
        )
        # Left: mascot on top, compact phases below
        layout["left"].split_column(
            Layout(name="mascot", size=17),
            Layout(name="phases", ratio=1),
        )
        # Right: ephemeral activity on top, spec artifacts below
        layout["right"].split_column(
            Layout(name="ephemeral", ratio=1),
            Layout(name="spec_data", ratio=1),
        )

        layout["header"].update(self._build_header())
        layout["mascot"].update(self._build_mascot())
        layout["phases"].update(self._build_phases())
        layout["right"]["ephemeral"].update(self._build_ephemeral())
        layout["right"]["spec_data"].update(self._build_spec_data())
        layout["log"].update(self._build_log())

        return layout

    def _build_header(self) -> Panel:
        elapsed = self._elapsed()
        if self._current_phase is not None:
            phase_name = dict(PHASES).get(self._current_phase, "?")
        else:
            phase_name = "Initializing"

        completed = sum(1 for s in self._phase_status.values() if s == "complete")
        total = len(PHASES)
        bar_len = 20
        filled = int((completed / total) * bar_len) if total else 0
        bar = f"[green]{'█' * filled}[/green][dim]{'░' * (bar_len - filled)}[/dim]"

        text = Text.from_markup(
            f"  [bold magenta]SWAGGER AGENT[/bold magenta]  │  "
            f"Phase [bold]{self._current_phase if self._current_phase is not None else '-'}[/bold]/{total} {phase_name}  │  "
            f"{bar} {completed}/{total}  │  "
            f"[bold]{elapsed:.1f}s[/bold]"
        )
        return Panel(text, style="bold blue", height=3)

    def _build_mascot(self) -> Panel:
        anim = _ANIM_SEQUENCE[self._frame_idx % len(_ANIM_SEQUENCE)]
        mascot_str = _FRAMES[self._mascot_level][anim]
        mascot_text = Text.from_ansi(mascot_str)

        return Panel(
            mascot_text,
            subtitle=f"[dim]lvl {self._mascot_level}[/dim]",
            border_style="magenta",
            padding=(0, 0),
        )

    def _build_phases(self) -> Panel:
        lines: list[str] = []
        for num, name in PHASES:
            status = self._phase_status.get(num, "pending")
            if status == "complete":
                lines.append(f" [green]✓[/green] [dim]{name}[/dim]")
            elif status == "active":
                lines.append(f" [yellow]►[/yellow] [bold]{name}[/bold]")
            else:
                lines.append(f" [dim]○ {name}[/dim]")

        return Panel(
            Text.from_markup("\n".join(lines)),
            border_style="cyan",
            padding=(0, 0),
        )

    def _build_ephemeral(self) -> Panel:
        """Transient activity panel — what's happening right now."""
        parts: list[str] = []

        if self._current_phase == 1:
            parts.append(f"[bold]Scout[/bold]  Turn {self._scout_turn}")
            parts.append("")
            all_tasks = ["identify_framework", "find_route_files", "find_servers"]
            for t in all_tasks:
                short = t.replace("identify_", "").replace("find_", "")
                if t in self._scout_completed:
                    parts.append(f"  [green]✓[/green] {short}")
                else:
                    parts.append(f"  [dim]○[/dim] {short}")

        elif self._current_phase == 2:
            with self._lock:
                done = self._route_files_done
                total = self._route_total
                in_flight = set(self._routes_in_flight)
            parts.append("[bold]Route Extraction[/bold]")
            parts.append("")
            if total:
                bar_len = 20
                filled = int((done / total) * bar_len)
                bar = f"[green]{'█' * filled}[/green][dim]{'░' * (bar_len - filled)}[/dim]"
                parts.append(f"  Files  {bar}  {done}/{total}")
            if len(in_flight) > 1:
                parts.append(f"  [yellow]►[/yellow] {len(in_flight)} files in flight")
            elif in_flight:
                short = next(iter(in_flight)).rsplit("/", 1)[-1]
                parts.append(f"  [yellow]►[/yellow] {short}")

        elif self._current_phase == 3:
            with self._lock:
                schema_round = self._schema_round
                schema_pending = self._schema_pending
                in_flight = set(self._schemas_in_flight)
            parts.append("[bold]Schema Resolution[/bold]")
            parts.append("")
            parts.append(f"  Round [bold]{schema_round}[/bold]  │  Pending [bold]{schema_pending}[/bold]")
            if len(in_flight) > 1:
                parts.append(f"  [yellow]►[/yellow] {len(in_flight)} schemas in flight")
            elif in_flight:
                parts.append(f"  [yellow]►[/yellow] {next(iter(in_flight))}")

        elif self._current_phase == 4:
            parts.append("[bold]Assembly[/bold]")
            parts.append("")
            parts.append(f"  Building OpenAPI 3.0 spec...")

        elif self._current_phase == 5:
            parts.append("[bold]Validation[/bold]")
            parts.append("")
            if self._validation_errors:
                parts.append(f"  [red]✗ {self._validation_errors} error(s)[/red]")
            else:
                parts.append("  [green]✓ No structural errors[/green]")
            if self._validation_warnings:
                parts.append(f"  [yellow]— {self._validation_warnings} warning(s)[/yellow]")

        else:
            parts.append("[dim]Initializing...[/dim]")

        content = "\n".join(parts) if parts else "[dim]Waiting...[/dim]"
        return Panel(content, title="[yellow]Current Activity[/yellow]", border_style="yellow")

    def _build_spec_data(self) -> Panel:
        """Accumulated spec artifacts — data that will become the swagger."""

        # ── Top: high-level summary info ──────────────────────────────────
        summary_parts: list[str] = []

        # Row 1: framework/language · servers
        identity: list[str] = []
        if self._spec_framework:
            identity.append(f"[bold]{self._spec_framework}[/bold][dim]/{self._spec_language}[/dim]")
        if self._spec_base_path:
            identity.append(self._spec_base_path)
        if self._spec_servers:
            identity.append(", ".join(self._spec_servers[:2]))
        if identity:
            summary_parts.append("  ".join(identity))

        # Row 2: counts — routes, endpoints, schemas, auth
        counts: list[str] = []
        if self._spec_route_files:
            counts.append(f"[dim]Routes:[/dim] {len(self._spec_route_files)} files")
        ep_n = len(self._spec_endpoints)
        if ep_n:
            counts.append(f"[dim]Endpoints:[/dim] {ep_n}")
        schema_n = len(self._spec_schema_names)
        unresolved_n = len(self._spec_unresolved_schemas)
        if schema_n or unresolved_n:
            s = f"[dim]Schemas:[/dim] [green]{schema_n}[/green]"
            if unresolved_n:
                s += f" [red]+{unresolved_n} unresolved[/red]"
            counts.append(s)
        if self._spec_security_schemes:
            counts.append(f"[dim]Auth:[/dim] [bold]{', '.join(sorted(self._spec_security_schemes))}[/bold]")
        if counts:
            summary_parts.append("  ".join(counts))

        # Issues — compact, only when problems exist
        issues: list[str] = []
        if self._failed_routes:
            issues.append(f"[red]{self._failed_routes} route fail(s)[/red]")
        if self._failed_schemas:
            issues.append(f"[red]{self._failed_schemas} schema fail(s)[/red]")
        if self._spec_unresolved_schemas:
            issues.append(f"[yellow]{len(self._spec_unresolved_schemas)} unresolved ref(s)[/yellow]")
        if self._validation_errors:
            issues.append(f"[red]{self._validation_errors} validation err(s)[/red]")
        if self._validation_warnings:
            issues.append(f"[yellow]{self._validation_warnings} warning(s)[/yellow]")
        if issues:
            summary_parts.append(f"[dim]Issues:[/dim] {', '.join(issues)}")

        if not summary_parts and not self._spec_endpoints and not self._spec_schema_names:
            return Panel("[dim]No spec data yet...[/dim]",
                         title="[green]Spec Data[/green]", border_style="green")

        summary_text = Text.from_markup("\n".join(summary_parts)) if summary_parts else Text("")

        # ── Bottom-left: Endpoints list ───────────────────────────────────
        MC = {"GET": "green", "POST": "yellow", "PUT": "blue",
              "PATCH": "cyan", "DELETE": "red", "HEAD": "dim", "OPTIONS": "dim"}
        ep_lines: list[str] = []
        # Reserve 2 lines for summary header, 2 for panel borders → available rows
        # We'll cap at a reasonable max and show overflow
        max_ep = 12
        for method, path, sec in self._spec_endpoints[:max_ep]:
            c = MC.get(method.upper(), "white")
            max_path = 24 if sec else 28
            display_path = path if len(path) <= max_path else path[:max_path - 3] + "..."
            lock = " 🔓" if sec else ""
            ep_lines.append(f"[{c}]{method:6s}[/{c}] {display_path}[dim]{lock}[/dim]")
        remaining_ep = len(self._spec_endpoints) - max_ep
        if remaining_ep > 0:
            ep_lines.append(f"[dim]+{remaining_ep} more[/dim]")
        ep_content = Text.from_markup("\n".join(ep_lines)) if ep_lines else Text.from_markup("[dim]—[/dim]")
        ep_panel = Panel(ep_content,
                         title=f"[dim]Endpoints[/dim] [bold]{ep_n}[/bold]",
                         border_style="dim green", padding=(0, 1))

        # ── Bottom-right: Schemas list ────────────────────────────────────
        schema_lines: list[str] = []
        max_sch = 12
        for name in self._spec_schema_names[:max_sch]:
            schema_lines.append(f"[green]{name}[/green]")
        remaining_sch = len(self._spec_schema_names) - max_sch
        if remaining_sch > 0:
            schema_lines.append(f"[dim]+{remaining_sch} more[/dim]")
        for name in self._spec_unresolved_schemas[:4]:
            schema_lines.append(f"[red]✗ {name}[/red]")
        remaining_unr = len(self._spec_unresolved_schemas) - 4
        if remaining_unr > 0:
            schema_lines.append(f"[dim red]+{remaining_unr} more unresolved[/dim red]")
        sch_label = f"[green]{schema_n}[/green]"
        if unresolved_n:
            sch_label += f" [red]+{unresolved_n}[/red]"
        sch_content = Text.from_markup("\n".join(schema_lines)) if schema_lines else Text.from_markup("[dim]—[/dim]")
        sch_panel = Panel(sch_content,
                          title=f"[dim]Schemas[/dim] {sch_label}",
                          border_style="dim green", padding=(0, 1))

        # ── Compose: summary on top, two columns on bottom ───────────────
        columns = Table.grid(expand=True)
        columns.add_column(ratio=1)
        columns.add_column(ratio=1)
        columns.add_row(ep_panel, sch_panel)

        outer = Table.grid(expand=True)
        outer.add_column(ratio=1)
        outer.add_row(summary_text)
        outer.add_row(columns)

        return Panel(outer, title="[green]Spec Data[/green]", border_style="green")

    def _build_log(self) -> Panel:
        if self._finished and self._completeness:
            return self._build_completeness_panel()

        with self._lock:
            visible = self._logs[-self._max_logs:]
        if visible:
            log_text = Text.from_markup("\n".join(visible))
        else:
            log_text = Text.from_markup("[dim]Waiting for activity...[/dim]")
        return Panel(log_text, title="[blue]Activity Log[/blue]", border_style="blue")

    def _build_completeness_panel(self) -> Panel:
        """Build the completeness checklist that replaces the activity log."""
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

        data = self._completeness.model_dump()
        elapsed = self._elapsed()

        table = Table(expand=True, show_header=False, box=None, padding=(0, 1))
        table.add_column("Check", ratio=1)
        table.add_column("Result", width=8, justify="center")

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
                table.add_row(label, "[green]✓[/green]")
            else:
                style = {"critical": "red", "warning": "yellow", "info": "dim"}.get(severity, "dim")
                icon = {"critical": "✗", "warning": "—", "info": "—"}.get(severity, "—")
                table.add_row(f"[{style}]{label}[/{style}]", f"[{style}]{icon}[/{style}]")

        title = f"[green]Spec Completeness[/green]  [dim]({elapsed:.1f}s)[/dim]"
        return Panel(table, title=title, border_style="green")
