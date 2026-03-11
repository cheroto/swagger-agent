"""Rich console event handler for standalone Scout testing.

Provides a live-updating dashboard during the run and post-run inspection
of artifacts, state snapshots, and the full turn-by-turn history.
"""

from __future__ import annotations

import json
import time

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.rule import Rule
from rich.syntax import Syntax
from rich.markdown import Markdown
from rich.columns import Columns

from swagger_agent.models import DiscoveryManifest
from swagger_agent.agents.scout.harness import ScoutEventHandler, ScoutRunRecord, StateUpdates


class RichScoutHandler(ScoutEventHandler):
    """Live-updating dashboard for Scout runs."""

    def __init__(self, verbose: bool = False, console: Console | None = None):
        self.console = console or Console(stderr=True)
        self.verbose = verbose
        self._live: Live | None = None
        self._start_time: float = 0.0

        # Dashboard state
        self._current_turn: int = 0
        self._total_tasks: int = 9
        self._remaining_tasks: list[str] = []
        self._completed_tasks: list[str] = []
        self._scratchpad: str = ""
        self._current_actions: list[dict] = []  # [{tool, status, summary}]
        self._turn_log: list[dict] = []  # [{turn, actions_summary, tasks_completed, duration}]
        self._findings: dict = {}  # key -> value for quick display

    def start(self) -> None:
        """Start the live display. Call before run_scout."""
        self._start_time = time.monotonic()
        self._live = Live(
            self._build_dashboard(),
            console=self.console,
            refresh_per_second=4,
            transient=False,
        )
        self._live.start()

    def stop(self) -> None:
        """Stop the live display. Call after run_scout."""
        if self._live:
            self._live.stop()
            self._live = None

    def _refresh(self) -> None:
        if self._live:
            self._live.update(self._build_dashboard())

    def _elapsed(self) -> str:
        elapsed = time.monotonic() - self._start_time
        return f"{elapsed:.1f}s"

    def _build_dashboard(self) -> Group:
        """Build the full dashboard layout."""
        parts = []

        # Header bar
        progress_done = self._total_tasks - len(self._remaining_tasks)
        bar_filled = int((progress_done / self._total_tasks) * 20) if self._total_tasks else 0
        bar = "[green]" + "█" * bar_filled + "[/green]" + "[dim]░[/dim]" * (20 - bar_filled)
        header = Text.from_markup(
            f"  Scout  │  Turn [bold]{self._current_turn}[/bold]  │  "
            f"Tasks {bar} {progress_done}/{self._total_tasks}  │  "
            f"Elapsed [bold]{self._elapsed()}[/bold]"
        )
        parts.append(Panel(header, style="blue"))

        # Two columns: left = tasks + findings, right = scratchpad
        left_parts = []

        # Tasks checklist
        task_table = Table(title="Tasks", show_header=False, box=None, padding=(0, 1), expand=True)
        task_table.add_column(width=2)
        task_table.add_column()
        for task in self._completed_tasks:
            task_table.add_row("[green]✓[/green]", f"[dim]{task}[/dim]")
        for task in self._remaining_tasks:
            task_table.add_row("[dim]○[/dim]", task)
        left_parts.append(task_table)

        # Key findings (compact)
        if self._findings:
            findings_table = Table(title="Findings", show_header=False, box=None, padding=(0, 1), expand=True)
            findings_table.add_column(style="bold", width=14)
            findings_table.add_column()
            for key, val in self._findings.items():
                findings_table.add_row(key, str(val))
            left_parts.append(findings_table)

        left_panel = Panel(Group(*left_parts), title="[cyan]State[/cyan]", border_style="cyan")

        # Scratchpad (right side)
        scratchpad_text = self._scratchpad or "(waiting for first turn)"
        if not self.verbose and len(scratchpad_text) > 500:
            scratchpad_text = scratchpad_text[:500] + "..."
        right_panel = Panel(
            scratchpad_text,
            title="[yellow]Scratchpad[/yellow]",
            border_style="yellow",
        )

        parts.append(Columns([left_panel, right_panel], equal=True, expand=True))

        # Current turn actions
        if self._current_actions:
            action_table = Table(title=f"Turn {self._current_turn} Actions", expand=True, box=None)
            action_table.add_column("Tool", width=16)
            action_table.add_column("Status", width=10)
            action_table.add_column("Result")
            for a in self._current_actions:
                status = a.get("status", "pending")
                style = {"done": "green", "error": "red", "running": "yellow"}.get(status, "dim")
                icon = {"done": "✓", "error": "✗", "running": "…"}.get(status, "○")
                action_table.add_row(
                    a["tool"],
                    f"[{style}]{icon} {status}[/{style}]",
                    a.get("summary", ""),
                )
            parts.append(Panel(action_table, border_style="blue"))

        # Turn history (compact)
        if self._turn_log:
            log_table = Table(title="Turn History", expand=True, box=None)
            log_table.add_column("#", width=3)
            log_table.add_column("Duration", width=8)
            log_table.add_column("Actions")
            log_table.add_column("Tasks Done", width=30)
            for entry in self._turn_log[-10:]:  # last 10 turns
                log_table.add_row(
                    str(entry["turn"]),
                    entry["duration"],
                    entry["actions"],
                    entry.get("tasks_done", ""),
                )
            parts.append(Panel(log_table, border_style="dim"))

        return Group(*parts)

    # --- Event handlers ---

    def on_turn_start(self, turn: int, remaining_tasks: list[str]) -> None:
        self._current_turn = turn
        self._remaining_tasks = list(remaining_tasks)
        self._current_actions = []

        # Rebuild completed tasks
        all_tasks = [
            "identify_framework", "find_entry_points", "find_route_files",
            "find_model_files", "identify_security", "find_servers",
            "find_error_handlers", "build_dependency_graph", "build_class_to_file_map",
        ]
        self._completed_tasks = [t for t in all_tasks if t not in remaining_tasks]
        self._refresh()

    def on_scratchpad_update(self, turn: int, scratchpad: str) -> None:
        self._scratchpad = scratchpad
        self._refresh()

    def on_state_update(self, turn: int, updates: StateUpdates, remaining_tasks: list[str]) -> None:
        self._remaining_tasks = list(remaining_tasks)
        all_tasks = [
            "identify_framework", "find_entry_points", "find_route_files",
            "find_model_files", "identify_security", "find_servers",
            "find_error_handlers", "build_dependency_graph", "build_class_to_file_map",
        ]
        self._completed_tasks = [t for t in all_tasks if t not in remaining_tasks]

        # Update findings display
        updates_dict = updates.model_dump(exclude_none=True)
        if "framework" in updates_dict:
            self._findings["Framework"] = updates_dict["framework"]
        if "language" in updates_dict:
            self._findings["Language"] = updates_dict["language"]
        if "entry_points" in updates_dict:
            self._findings["Entry points"] = ", ".join(updates_dict["entry_points"])
        if "route_files" in updates_dict:
            count = len(updates_dict["route_files"])
            self._findings["Route files"] = f"+{count} (total: {len(remaining_tasks) + count})"
        if "model_files" in updates_dict:
            self._findings["Model files"] = f"+{len(updates_dict['model_files'])}"
        if "security_schemes" in updates_dict:
            names = [s.get("name", "?") for s in updates_dict["security_schemes"]]
            self._findings["Security"] = ", ".join(names)
        if "servers" in updates_dict:
            self._findings["Servers"] = ", ".join(updates_dict["servers"])

        self._refresh()

    def on_tool_execute(self, turn: int, tool: str, args: dict) -> None:
        args_short = json.dumps(args, default=str)
        if len(args_short) > 60:
            args_short = args_short[:57] + "..."
        self._current_actions.append({
            "tool": f"{tool}({args_short})",
            "status": "running",
            "summary": "",
        })
        self._refresh()

    def on_tool_result(self, turn: int, tool: str, summary: str) -> None:
        # Update the last "running" action for this tool
        for a in reversed(self._current_actions):
            if a["status"] == "running":
                a["status"] = "done"
                a["summary"] = summary
                break
        self._refresh()

    def on_tool_error(self, turn: int, tool: str, error: str) -> None:
        for a in reversed(self._current_actions):
            if a["status"] == "running":
                a["status"] = "error"
                a["summary"] = error
                break
        self._refresh()

    def on_manifest(self, manifest: DiscoveryManifest) -> None:
        # Add final turn to log before stopping
        self._findings["Framework"] = manifest.framework
        self._findings["Language"] = manifest.language
        self._findings["Route files"] = str(len(manifest.route_files))
        self._findings["Model files"] = str(len(manifest.model_files))
        schemes = ", ".join(s.name for s in manifest.security_schemes) or "-"
        self._findings["Security"] = schemes
        self._findings["Servers"] = ", ".join(manifest.servers) or "-"
        self._refresh()

    def on_max_turns(self, turn: int) -> None:
        self._refresh()

    def on_llm_error(self, turn: int, error: Exception, retries_left: int) -> None:
        self._current_actions.append({
            "tool": "LLM",
            "status": "error",
            "summary": f"{error} ({retries_left} retries left)",
        })
        self._refresh()

    def _record_turn_log(self, turn: int, duration_ms: float, tasks_done: list[str]) -> None:
        """Called by cli.py after each turn to record in history."""
        actions_str = ", ".join(a["tool"].split("(")[0] for a in self._current_actions)
        self._turn_log.append({
            "turn": turn,
            "duration": f"{duration_ms:.0f}ms",
            "actions": actions_str,
            "tasks_done": ", ".join(tasks_done) if tasks_done else "-",
        })


# --- Post-run inspection (same as before, for use after live display stops) ---


def print_run_summary(record: ScoutRunRecord, console: Console | None = None) -> None:
    console = console or Console(stderr=True)
    console.print(Rule(" Run Summary ", style="bold blue"))

    summary_table = Table(show_header=False, box=None, padding=(0, 2))
    summary_table.add_column(style="bold")
    summary_table.add_column()
    summary_table.add_row("Target", record.target_dir)
    summary_table.add_row("Turns", str(len(record.turns)))
    summary_table.add_row("Duration", f"{record.total_duration_ms:.0f}ms")
    summary_table.add_row("Termination", record.termination_reason)
    summary_table.add_row("Files touched", str(len(record.trace.files_touched)))
    console.print(summary_table)
    console.print()


def print_manifest(record: ScoutRunRecord, console: Console | None = None) -> None:
    console = console or Console(stderr=True)
    console.print(Rule(" Discovery Manifest ", style="bold green"))
    manifest_json = json.dumps(record.manifest, indent=2, default=str)
    console.print(Syntax(manifest_json, "json", theme="monokai", line_numbers=False))
    console.print()


def print_final_state(record: ScoutRunRecord, console: Console | None = None) -> None:
    console = console or Console(stderr=True)
    console.print(Rule(" Final State ", style="bold yellow"))
    state_json = json.dumps(record.final_state, indent=2, default=str)
    console.print(Syntax(state_json, "json", theme="monokai", line_numbers=False))
    console.print()


def print_turn_detail(record: ScoutRunRecord, turn_number: int, console: Console | None = None) -> None:
    console = console or Console(stderr=True)
    if turn_number < 1 or turn_number > len(record.turns):
        console.print(f"[red]Turn {turn_number} not found (run had {len(record.turns)} turns)[/red]")
        return

    turn = record.turns[turn_number - 1]
    console.print(Rule(f" Turn {turn.turn} Detail ", style="bold cyan"))
    console.print(f"[dim]Duration: {turn.duration_ms:.0f}ms[/dim]")
    console.print(f"[dim]Remaining tasks: {turn.remaining_tasks}[/dim]")

    console.print(Panel(Markdown(turn.scratchpad), title="[yellow]Scratchpad[/yellow]", border_style="yellow"))

    if turn.state_updates:
        console.print(Panel(
            Syntax(json.dumps(turn.state_updates, indent=2, default=str), "json", theme="monokai"),
            title="[cyan]State Updates[/cyan]",
            border_style="cyan",
        ))

    for action in turn.actions:
        tool = action["tool"]
        summary = action.get("summary", "")
        error = action.get("error")
        if error:
            console.print(f"  [red]✗ {tool}:[/red] {error}")
        else:
            console.print(f"  [blue]{tool}[/blue] -> {summary}")
        if "result" in action:
            result_str = action["result"]
            if len(result_str) > 2000:
                result_str = result_str[:2000] + "\n... (truncated)"
            console.print(Syntax(result_str, "json", theme="monokai", line_numbers=False))
    console.print()


def print_all_turns(record: ScoutRunRecord, console: Console | None = None) -> None:
    console = console or Console(stderr=True)
    console.print(Rule(" Turn Timeline ", style="bold cyan"))

    table = Table()
    table.add_column("#", style="bold", width=4)
    table.add_column("Duration", width=8)
    table.add_column("Actions", width=50)
    table.add_column("Tasks Left", width=8, justify="right")
    table.add_column("Scratchpad", width=60)

    for turn in record.turns:
        actions_str = ", ".join(
            f"{a['tool']}({'✗' if 'error' in a else a.get('summary', 'ok')})"
            for a in turn.actions
        )
        scratchpad_preview = turn.scratchpad.split("\n")[0][:60]
        table.add_row(
            str(turn.turn),
            f"{turn.duration_ms:.0f}ms",
            actions_str,
            str(len(turn.remaining_tasks)),
            scratchpad_preview,
        )

    console.print(table)
    console.print()


def print_trace(record: ScoutRunRecord, console: Console | None = None) -> None:
    console = console or Console(stderr=True)
    console.print(Rule(" Deterministic Trace ", style="bold magenta"))

    table = Table()
    table.add_column("Turn", width=4)
    table.add_column("Tool", width=16)
    table.add_column("Args", width=50)
    table.add_column("Summary", width=30)

    for entry in record.trace.tool_history:
        args_short = entry.args
        if len(args_short) > 50:
            args_short = args_short[:47] + "..."
        table.add_row(str(entry.turn), entry.tool, args_short, entry.summary)

    console.print(table)
    console.print()

    if record.trace.files_touched:
        console.print("[bold]Files touched:[/bold]")
        for f in record.trace.files_touched:
            console.print(f"  {f}")
        console.print()
