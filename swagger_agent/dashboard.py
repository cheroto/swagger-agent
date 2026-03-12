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
from swagger_agent.models import DiscoveryManifest


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
    (1, "Scout"),
    (2, "Route Extraction"),
    (3, "Schema Resolution"),
    (4, "Assembly"),
    (5, "Validation"),
]

_LEVEL_FOR_PHASE = {1: 1, 2: 2, 3: 3, 4: 3, 5: 3}  # scout=1, routes=2, schemas+=3
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
        self._current_phase = 0
        self._phase_status: dict[int, str] = {}  # "pending"|"active"|"complete"
        self._phase_summary: dict[int, str] = {}

        # Scout state (for detail panel)
        self._scout_turn = 0
        self._scout_remaining: list[str] = []
        self._scout_completed: list[str] = []
        self._scout_findings: dict[str, str] = {}

        # Route extraction state
        self._route_current = ""
        self._route_index = 0
        self._route_total = 0
        self._route_endpoints_total = 0
        self._route_files_done = 0

        # Schema state
        self._schema_round = 0
        self._schema_pending = 0
        self._schema_current = ""
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
        self._route_current = file
        self._route_index = index
        self._route_total = total
        short = file.rsplit("/", 1)[-1]
        self._log("ROUTE", f"Extracting [bold]{short}[/bold] ({index}/{total})", "yellow")
        self._refresh()

    def route_complete(self, file: str, endpoints: int, duration_ms: float) -> None:
        self._route_endpoints_total += endpoints
        self._route_files_done += 1
        short = file.rsplit("/", 1)[-1]
        self._log("ROUTE", f"[green]✓[/green] {short} → {endpoints} endpoint(s) ({duration_ms:.0f}ms)", "green")
        self._refresh()

    def route_endpoints_discovered(self, descriptor) -> None:
        """Feed extracted endpoint details into the spec artifacts panel.

        Called by pipeline after each successful route extraction.
        ``descriptor`` is an EndpointDescriptor pydantic model.
        """
        for ep in descriptor.endpoints:
            sec = ", ".join(ep.security) if ep.security else None
            self._spec_endpoints.append((ep.method, ep.path, sec))
            if ep.security:
                for s in ep.security:
                    self._spec_security_schemes.add(s)
        self._refresh()

    def route_failed(self, file: str, error: str) -> None:
        self._failed_routes += 1
        short = file.rsplit("/", 1)[-1]
        self._log("ROUTE", f"[red]✗[/red] {short}: {error}", "red")
        self._refresh()

    # ── Schema loop events (callback for run_schema_loop) ─────────────────

    def schema_event(self, event: str, **kwargs) -> None:
        if event == "ctags_built":
            self._log("SCHEMA", f"Indexed {kwargs.get('count', 0)} type definitions", "dim")
        elif event == "round_start":
            self._schema_round = kwargs.get("round", 0)
            self._schema_pending = kwargs.get("pending", 0)
            self._log("SCHEMA", f"Round {self._schema_round} ({self._schema_pending} pending)", "yellow")
        elif event == "already_extracted":
            self._log("SCHEMA", f"[dim]{kwargs.get('file', '?')} already extracted[/dim]", "dim")
        elif event == "resolving":
            name = kwargs.get("name", "?")
            file = kwargs.get("file")
            self._schema_current = name
            if file:
                short = str(file).rsplit("/", 1)[-1]
                self._log("SCHEMA", f"{name} → [bold]{short}[/bold]", "cyan")
            else:
                self._log("SCHEMA", f"[red]Could not resolve[/red] {name}", "red")
                self._schema_unresolved += 1
                if name not in self._spec_unresolved_schemas:
                    self._spec_unresolved_schemas.append(name)
        elif event == "extracted":
            count = kwargs.get("count", 0)
            duration = kwargs.get("duration_ms", 0)
            self._schema_resolved += count
            file = kwargs.get("file", "")
            short = str(file).rsplit("/", 1)[-1]
            self._log("SCHEMA", f"[green]✓[/green] {short} → {count} schema(s) ({duration:.0f}ms)", "green")
            # Track individual schema names
            schema_names = kwargs.get("schema_names", [])
            for sn in schema_names:
                if sn not in self._spec_schema_names:
                    self._spec_schema_names.append(sn)
        elif event == "extract_failed":
            name = kwargs.get("name", "?")
            self._schema_unresolved += 1
            self._failed_schemas += 1
            self._log("SCHEMA", f"[red]✗[/red] {name}: {kwargs.get('error', '')}", "red")
            if name not in self._spec_unresolved_schemas:
                self._spec_unresolved_schemas.append(name)
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
        if self._current_phase:
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
            f"Phase [bold]{self._current_phase or '-'}[/bold]/5 {phase_name}  │  "
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
            done = self._route_files_done
            total = self._route_total
            parts.append("[bold]Route Extraction[/bold]")
            parts.append("")
            if total:
                bar_len = 20
                filled = int((done / total) * bar_len)
                bar = f"[green]{'█' * filled}[/green][dim]{'░' * (bar_len - filled)}[/dim]"
                parts.append(f"  Files  {bar}  {done}/{total}")
            if self._route_current:
                short = self._route_current.rsplit("/", 1)[-1]
                parts.append(f"  [yellow]►[/yellow] {short}")

        elif self._current_phase == 3:
            parts.append("[bold]Schema Resolution[/bold]")
            parts.append("")
            parts.append(f"  Round [bold]{self._schema_round}[/bold]  │  Pending [bold]{self._schema_pending}[/bold]")
            if self._schema_current:
                parts.append(f"  [yellow]►[/yellow] {self._schema_current}")

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
        parts: list[str] = []

        # Identity line: framework (language) · base · servers — all on one line
        identity: list[str] = []
        if self._spec_framework:
            identity.append(f"[bold]{self._spec_framework}[/bold][dim]/{self._spec_language}[/dim]")
        if self._spec_base_path:
            identity.append(self._spec_base_path)
        if self._spec_servers:
            identity.append(", ".join(self._spec_servers[:2]))
        if identity:
            parts.append("  ".join(identity))

        # Counts line: route files · security — compact
        counts: list[str] = []
        if self._spec_route_files:
            counts.append(f"[dim]Routes:[/dim] {len(self._spec_route_files)} files")
        if self._spec_security_schemes:
            counts.append(f"[dim]Auth:[/dim] [bold]{', '.join(sorted(self._spec_security_schemes))}[/bold]")
        if counts:
            parts.append("  ".join(counts))

        # Endpoints — compact table-like rows, truncate long paths
        if self._spec_endpoints:
            MC = {"GET": "green", "POST": "yellow", "PUT": "blue",
                  "PATCH": "cyan", "DELETE": "red", "HEAD": "dim", "OPTIONS": "dim"}
            parts.append("")
            parts.append(f"[dim]Endpoints[/dim] [bold]{len(self._spec_endpoints)}[/bold]")
            max_show = 8
            for method, path, sec in self._spec_endpoints[:max_show]:
                c = MC.get(method.upper(), "white")
                # Truncate long paths to keep lines short
                display_path = path if len(path) <= 40 else path[:37] + "..."
                lock = " 🔓" if sec else ""
                parts.append(f"  [{c}]{method:6s}[/{c}] {display_path}[dim]{lock}[/dim]")
            remaining = len(self._spec_endpoints) - max_show
            if remaining > 0:
                parts.append(f"  [dim]+{remaining} more[/dim]")

        # Schemas — inline comma-separated list instead of one-per-line
        if self._spec_schema_names or self._spec_unresolved_schemas:
            resolved_n = len(self._spec_schema_names)
            unresolved_n = len(self._spec_unresolved_schemas)
            parts.append("")
            label = f"[dim]Schemas[/dim] [green]{resolved_n}[/green]"
            if unresolved_n:
                label += f" [red]+{unresolved_n} unresolved[/red]"
            parts.append(label)
            if self._spec_schema_names:
                names = ", ".join(self._spec_schema_names[:12])
                if len(self._spec_schema_names) > 12:
                    names += f", [dim]+{len(self._spec_schema_names) - 12} more[/dim]"
                parts.append(f"  [green]{names}[/green]")
            if self._spec_unresolved_schemas:
                unames = ", ".join(self._spec_unresolved_schemas[:4])
                parts.append(f"  [red]✗ {unames}[/red]")

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
            parts.append("")
            parts.append(f"[dim]Issues:[/dim] {', '.join(issues)}")

        if not parts:
            parts.append("[dim]No spec data yet...[/dim]")

        content = "\n".join(parts)
        return Panel(content, title="[green]Spec Data[/green]", border_style="green")

    def _build_log(self) -> Panel:
        with self._lock:
            visible = self._logs[-self._max_logs:]
        if visible:
            log_text = Text.from_markup("\n".join(visible))
        else:
            log_text = Text.from_markup("[dim]Waiting for activity...[/dim]")
        return Panel(log_text, title="[blue]Activity Log[/blue]", border_style="blue")
