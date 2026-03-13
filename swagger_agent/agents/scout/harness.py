"""Scout agent harness — stateless turn architecture.

Rebuilds the prompt from three state layers at every turn:
1. Deterministic trace (harness-managed, append-only)
2. Scratchpad (LLM-managed, full rewrite each turn — structurally enforced)
3. Structured findings (LLM-managed, accumulating — structurally enforced)

No conversation history is kept. One LLM inference per turn.
Scratchpad and state updates are required/optional fields in the structured
response — not tool calls the LLM can forget.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import instructor
from openai import OpenAI
from pydantic import BaseModel, Field

from swagger_agent.config import LLMConfig
from swagger_agent.models import (
    DiscoveryManifest,
    ScoutWorkingState,
)
from swagger_agent.agents.scout.prompt import SCOUT_SYSTEM_PROMPT
from swagger_agent.agents.scout.tools import build_scout_tools
from swagger_agent.infra.prescan import PrescanResult, prescan_to_scratchpad
from swagger_agent.telemetry import LLMCall, Telemetry, measure_messages

logger = logging.getLogger("swagger_agent.scout")

MAX_SCOUT_TURNS = 50


# --- Structured turn response (enforced by instructor) ---


class StateUpdates(BaseModel):
    """Structured findings to merge into working state."""

    framework: str | None = None
    language: str | None = None
    route_files: list[str] | None = None
    servers: list[str] | None = None
    base_path: str | None = None
    completed_tasks: list[str] = Field(
        default_factory=list,
        description="Task names to mark as complete and remove from remaining_tasks",
    )


class ScoutAction(BaseModel):
    """A single tool call from the Scout."""

    tool: str = Field(description="Tool name: glob, grep, read_file_head, read_file_range, or write_artifact")
    arguments: dict = Field(description="Tool arguments as a JSON object")


class ScoutTurnResponse(BaseModel):
    """The Scout's complete response for a single turn.

    scratchpad and state_updates are structurally enforced fields.
    The agent cannot skip reflection or forget to persist findings.
    """

    scratchpad: str = Field(
        description=(
            "MANDATORY. Reflect on the results from the previous turn. "
            "Record key findings, open questions, and your plan for this turn. "
            "~1500 token budget. This is your working memory between turns."
        ),
    )
    state_updates: StateUpdates = Field(
        default_factory=StateUpdates,
        description=(
            "Persist new findings into structured state. Include completed_tasks "
            "to check off finished items. "
            "Lists are appended (deduped), scalars overwrite."
        ),
    )
    actions: list[ScoutAction] = Field(
        min_length=1,
        max_length=4,
        description="1-4 tool calls to execute this turn: glob, grep, read_file_head, read_file_range, or write_artifact.",
    )



# --- Deterministic Trace ---


@dataclass
class ToolHistoryEntry:
    turn: int
    tool: str
    args: str
    summary: str


@dataclass
class DeterministicTrace:
    turn_count: int = 0
    tool_history: list[ToolHistoryEntry] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)

    def record(self, turn: int, tool_name: str, args: dict, result: Any) -> None:
        summary = _summarize_tool_result(tool_name, result)
        args_str = json.dumps(args, default=str)
        self.tool_history.append(ToolHistoryEntry(
            turn=turn, tool=tool_name, args=args_str, summary=summary,
        ))
        if tool_name in ("read_file_head", "read_file_range"):
            path = args.get("path", "")
            if path and path not in self.files_touched:
                self.files_touched.append(path)
        elif tool_name == "grep":
            if isinstance(result, list):
                for match in result:
                    f = match.get("file", "") if isinstance(match, dict) else ""
                    if f and f not in self.files_touched:
                        self.files_touched.append(f)

    def to_text(self) -> str:
        lines = [f"Turn count: {self.turn_count}"]
        lines.append("Tool history:")
        for entry in self.tool_history:
            lines.append(f"  [{entry.turn}] {entry.tool}({entry.args}) -> {entry.summary}")
        lines.append(f"Files touched: {', '.join(self.files_touched) if self.files_touched else 'none'}")
        return "\n".join(lines)


def _summarize_tool_result(tool_name: str, result: Any) -> str:
    if tool_name == "glob":
        if isinstance(result, list):
            return f"{len(result)} files matched"
        return "error"
    elif tool_name == "grep":
        if isinstance(result, list):
            if result and isinstance(result[0], dict) and "error" in result[0]:
                return f"error: {result[0]['error']}"
            return f"{len(result)} matches"
        return "error"
    elif tool_name in ("read_file_head", "read_file_range"):
        if isinstance(result, str):
            line_count = result.count("\n") + 1 if result else 0
            return f"{line_count} lines read"
        return "error"
    return "ok"


# --- Run Record (captures everything for post-run inspection) ---


@dataclass
class TurnRecord:
    """Full record of a single turn for post-run inspection."""

    turn: int
    scratchpad: str
    state_updates: dict | None
    actions: list[dict]  # [{tool, args, result, summary, error?}]
    state_snapshot: dict  # state after this turn
    remaining_tasks: list[str]
    duration_ms: float


@dataclass
class ScoutRunRecord:
    """Complete record of a Scout run. Inspect after run_scout returns."""

    target_dir: str = ""
    turns: list[TurnRecord] = field(default_factory=list)
    trace: DeterministicTrace = field(default_factory=DeterministicTrace)
    final_state: dict = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)
    total_duration_ms: float = 0.0
    termination_reason: str = ""  # "write_artifact", "max_turns", "error"


# --- State Management ---


# Tasks that require non-empty data before they can be marked complete.
# Maps task name → list of state fields that must be non-empty (truthy).
_TASK_COMPLETION_GUARDS: dict[str, list[str]] = {
    "find_route_files": ["route_files"],
    "identify_framework": ["framework"],
}


def _normalize_path(path: str, target_dir: str) -> str:
    """Normalize a path to be relative to target_dir.

    Handles cases where the LLM includes the target_dir prefix,
    uses absolute paths, or includes ./ prefixes.
    """
    # Strip leading/trailing whitespace
    path = path.strip()
    # Normalize the path (resolve .., ., double slashes)
    path = os.path.normpath(path)
    # If it's an absolute path, make it relative to target_dir
    abs_target = os.path.normpath(os.path.abspath(target_dir))
    abs_path = os.path.normpath(os.path.abspath(path)) if os.path.isabs(path) else None
    if abs_path and abs_path.startswith(abs_target + os.sep):
        return os.path.relpath(abs_path, abs_target)
    # If it starts with the target_dir as a relative prefix, strip it
    norm_target = os.path.normpath(target_dir)
    if path.startswith(norm_target + os.sep):
        return os.path.relpath(path, norm_target)
    # Strip leading slash (shouldn't happen after normpath, but defensive)
    return path.lstrip(os.sep)


def apply_state_update(
    state: ScoutWorkingState,
    updates: StateUpdates,
    target_dir: str,
) -> tuple[ScoutWorkingState, list[str]]:
    """Merge LLM-provided updates into the working state.

    Lists append (deduped), scalars overwrite. completed_tasks are removed
    from remaining_tasks — but only if the corresponding data fields have
    been populated (guards prevent marking a task done without evidence).

    Returns (new_state, rejections) where rejections is a list of
    human-readable messages for guard failures (surfaced to the LLM).
    """
    data = state.model_dump(by_alias=True)
    updates_dict = updates.model_dump(exclude_none=True)
    rejections: list[str] = []

    # First, merge data fields (before checking guards)
    for key, value in updates_dict.items():
        if key == "completed_tasks":
            continue
        if key not in data:
            continue

        current = data[key]
        if isinstance(current, list) and isinstance(value, list):
            seen = set(current)
            for item in value:
                # Normalize file paths in route_files
                if key == "route_files":
                    item = _normalize_path(item, target_dir)
                if item not in seen:
                    current.append(item)
                    seen.add(item)
        else:
            data[key] = value

    # Then, remove completed tasks — but only if guards pass
    completed = updates.completed_tasks
    if completed:
        actually_completed = []
        for task in completed:
            required_fields = _TASK_COMPLETION_GUARDS.get(task)
            if required_fields is None:
                # No guard — accept unconditionally
                actually_completed.append(task)
                continue
            # Check all required fields are non-empty
            if all(data.get(f) for f in required_fields):
                actually_completed.append(task)
            else:
                msg = (
                    f"REJECTED: '{task}' cannot be marked complete — "
                    f"required fields {required_fields} are empty. "
                    f"You must include {required_fields} in state_updates before marking this task complete."
                )
                rejections.append(msg)
                logger.warning(
                    "Rejected completion of '%s': required fields %s are empty",
                    task, required_fields,
                )

        data["remaining_tasks"] = [
            t for t in data.get("remaining_tasks", []) if t not in actually_completed
        ]

    return ScoutWorkingState.model_validate(data), rejections


def state_to_manifest(state: ScoutWorkingState) -> DiscoveryManifest:
    """Convert working state to a DiscoveryManifest (fallback path)."""
    return DiscoveryManifest(
        framework=state.framework or "unknown",
        language=state.language or "unknown",
        route_files=state.route_files,
        servers=state.servers,
        base_path=state.base_path,
    )


# --- Prompt Builder ---


def build_turn_messages(
    target_dir: str,
    trace: DeterministicTrace,
    state: ScoutWorkingState,
    last_action_results: list[dict] | None,
) -> list[dict]:
    """Build the full prompt for a single turn from the three state layers."""
    messages = [
        {"role": "system", "content": SCOUT_SYSTEM_PROMPT},
    ]

    parts = [f"Target directory: {target_dir}"]

    parts.append("\n## Deterministic Trace\n")
    parts.append(trace.to_text())

    parts.append("\n## Scratchpad\n")
    parts.append(state.scratchpad if state.scratchpad else "(empty - first turn)")

    parts.append("\n## Structured Findings\n")
    findings = state.model_dump(by_alias=True, exclude={"scratchpad", "remaining_tasks"})
    parts.append(json.dumps(findings, indent=2, default=str))

    parts.append("\n## Remaining Tasks\n")
    if state.remaining_tasks:
        for task in state.remaining_tasks:
            parts.append(f"- {task}")
    else:
        parts.append("ALL TASKS COMPLETE - call write_artifact now.")

    if last_action_results is not None:
        parts.append("\n## Results from Last Turn\n")
        for r in last_action_results:
            result_str = r.get("result", "")
            if len(result_str) > 4000:
                result_str = result_str[:4000] + "\n... (truncated)"
            parts.append(f"### {r['tool']}({r['args_summary']})\n{result_str}\n")

    messages.append({"role": "user", "content": "\n".join(parts)})
    return messages


# --- Event handler protocol ---


class ScoutEventHandler:
    """Override methods to receive Scout lifecycle events.

    Default implementation logs to the swagger_agent.scout logger.
    """

    def on_turn_start(self, turn: int, remaining_tasks: list[str]) -> None:
        logger.info("Scout turn %d, remaining tasks: %s", turn, remaining_tasks)

    def on_scratchpad_update(self, turn: int, scratchpad: str) -> None:
        logger.debug("Scratchpad updated (%d chars)", len(scratchpad))

    def on_state_update(self, turn: int, updates: StateUpdates, remaining_tasks: list[str]) -> None:
        logger.debug("State updated, remaining: %s", remaining_tasks)

    def on_tool_execute(self, turn: int, tool: str, args: dict) -> None:
        logger.debug("Executing %s(%s)", tool, json.dumps(args, default=str))

    def on_tool_result(self, turn: int, tool: str, summary: str) -> None:
        logger.info("[%d] %s -> %s", turn, tool, summary)

    def on_tool_error(self, turn: int, tool: str, error: str) -> None:
        logger.error("[%d] %s failed: %s", turn, tool, error)

    def on_manifest(self, manifest: DiscoveryManifest) -> None:
        logger.info(
            "Scout complete: %s/%s, %d routes",
            manifest.framework, manifest.language,
            len(manifest.route_files),
        )

    def on_max_turns(self, turn: int) -> None:
        logger.warning("Scout hit max turns (%d), serializing current state", turn)

    def on_llm_error(self, turn: int, error: Exception, retries_left: int) -> None:
        logger.error("Scout turn %d LLM error (%d retries left): %s", turn, retries_left, error)


# --- Main Harness ---


def run_scout(
    target_dir: str,
    config: LLMConfig | None = None,
    event_handler: ScoutEventHandler | None = None,
    prescan: PrescanResult | None = None,
    telemetry: Telemetry | None = None,
) -> tuple[DiscoveryManifest, ScoutRunRecord]:
    """Run the Scout agent against a target directory.

    Args:
        target_dir: Path to the target project directory.
        config: LLM configuration. Uses defaults if None.
        event_handler: Event handler for lifecycle callbacks.
        prescan: Optional pre-scan results to seed the Scout's initial state.
            When provided, the Scout starts with tentative findings already
            populated and focuses on confirmation rather than exploration.

    Returns (manifest, run_record). The run_record contains the full
    history of turns, states, and tool results for post-run inspection.
    """
    if config is None:
        config = LLMConfig()
    if event_handler is None:
        event_handler = ScoutEventHandler()

    from swagger_agent.config import _INSTRUCTOR_MODES
    base_url, model = config.for_agent("scout")
    raw_client = OpenAI(base_url=base_url, api_key=config.llm_api_key)
    mode = _INSTRUCTOR_MODES.get(config.instructor_mode.lower(), instructor.Mode.TOOLS)
    client = instructor.from_openai(raw_client, mode=mode)

    tools = build_scout_tools(target_dir)

    if prescan is not None:
        state = ScoutWorkingState(
            framework=prescan.framework,
            language=prescan.language,
            route_files=list(prescan.route_files),
            servers=list(prescan.servers),
            base_path=prescan.base_path,
            scratchpad=prescan_to_scratchpad(prescan),
        )
        logger.info(
            "Scout seeded with prescan: framework=%s, %d route(s)",
            prescan.framework, len(prescan.route_files),
        )
    else:
        state = ScoutWorkingState()
    trace = DeterministicTrace()
    last_action_results: list[dict] | None = None

    run_record = ScoutRunRecord(target_dir=target_dir, trace=trace)
    run_start = time.monotonic()

    for turn in range(1, MAX_SCOUT_TURNS + 1):
        turn_start = time.monotonic()
        trace.turn_count = turn
        event_handler.on_turn_start(turn, state.remaining_tasks)

        messages = build_turn_messages(target_dir, trace, state, last_action_results)

        llm_input_chars = measure_messages(messages)
        llm_start = time.monotonic()

        turn_response = client.chat.completions.create(
            model=model,
            response_model=ScoutTurnResponse,
            max_retries=config.instructor_max_retries,
            messages=messages,
            temperature=config.llm_temperature,
            max_tokens=config.llm_max_tokens,
        )

        llm_duration_ms = (time.monotonic() - llm_start) * 1000
        if telemetry:
            output_json = turn_response.model_dump_json()
            telemetry.record(LLMCall(
                agent="scout",
                phase=f"turn_{turn}",
                model=model,
                input_chars=llm_input_chars,
                output_chars=len(output_json),
                duration_ms=llm_duration_ms,
                target_file=target_dir,
                timestamp=llm_start,
            ))

        # 1. Apply scratchpad (always present — enforced by Pydantic schema)
        state.scratchpad = turn_response.scratchpad
        event_handler.on_scratchpad_update(turn, state.scratchpad)

        # 2. Apply state updates (structurally enforced — always present)
        state, rejections = apply_state_update(
            state, turn_response.state_updates, target_dir,
        )
        state_updates_dict = turn_response.state_updates.model_dump(exclude_none=True)
        event_handler.on_state_update(turn, turn_response.state_updates, state.remaining_tasks)

        # 3. Execute actions (exploration tools + write_artifact only)
        last_action_results = []

        # Surface guard rejections so the LLM can self-correct
        for rejection_msg in rejections:
            last_action_results.append({
                "tool": "state_update",
                "args": {},
                "args_summary": "guard",
                "result": rejection_msg,
                "summary": "guard rejection",
            })
        turn_action_records: list[dict] = []

        for action in turn_response.actions:
            tool_name = action.tool
            tool_args = action.arguments

            # Intercept write_artifact
            if tool_name == "write_artifact":
                # Always build manifest from accumulated state — never trust
                # LLM-provided data which may contain hallucinated additions.
                manifest = state_to_manifest(state)
                event_handler.on_manifest(manifest)

                turn_action_records.append({
                    "tool": "write_artifact",
                    "args": tool_args,
                    "args_summary": "discovery_manifest",
                    "result": "manifest written",
                    "summary": "manifest written",
                })

                turn_duration = (time.monotonic() - turn_start) * 1000
                run_record.turns.append(TurnRecord(
                    turn=turn,
                    scratchpad=turn_response.scratchpad,
                    state_updates=state_updates_dict,
                    actions=turn_action_records,
                    state_snapshot=state.model_dump(by_alias=True),
                    remaining_tasks=list(state.remaining_tasks),
                    duration_ms=turn_duration,
                ))
                run_record.final_state = state.model_dump(by_alias=True)
                run_record.manifest = manifest.model_dump(by_alias=True, exclude_none=True)
                run_record.total_duration_ms = (time.monotonic() - run_start) * 1000
                run_record.termination_reason = "write_artifact"
                return manifest, run_record

            # Execute exploration tools
            if tool_name not in tools:
                event_handler.on_tool_error(turn, tool_name, f"unknown tool '{tool_name}'")
                rec = {
                    "tool": tool_name,
                    "args": tool_args,
                    "args_summary": str(tool_args),
                    "result": f"Error: unknown tool '{tool_name}'",
                    "summary": "error: unknown tool",
                    "error": f"unknown tool '{tool_name}'",
                }
                last_action_results.append(rec)
                turn_action_records.append(rec)
                continue

            event_handler.on_tool_execute(turn, tool_name, tool_args)
            tool = tools[tool_name]
            try:
                result = tool.execute(**tool_args)
            except Exception as e:
                event_handler.on_tool_error(turn, tool_name, str(e))
                rec = {
                    "tool": tool_name,
                    "args": tool_args,
                    "args_summary": json.dumps(tool_args, default=str),
                    "result": f"Error: {e}",
                    "summary": f"error: {e}",
                    "error": str(e),
                }
                last_action_results.append(rec)
                turn_action_records.append(rec)
                continue

            trace.record(turn, tool_name, tool_args, result)
            summary = _summarize_tool_result(tool_name, result)
            event_handler.on_tool_result(turn, tool_name, summary)

            if isinstance(result, (list, dict)):
                result_str = json.dumps(result, indent=2, default=str)
            else:
                result_str = str(result)

            rec = {
                "tool": tool_name,
                "args": tool_args,
                "args_summary": json.dumps(tool_args, default=str),
                "result": result_str,
                "summary": summary,
            }
            last_action_results.append(rec)
            turn_action_records.append(rec)

        turn_duration = (time.monotonic() - turn_start) * 1000
        run_record.turns.append(TurnRecord(
            turn=turn,
            scratchpad=turn_response.scratchpad,
            state_updates=state_updates_dict,
            actions=turn_action_records,
            state_snapshot=state.model_dump(by_alias=True),
            remaining_tasks=list(state.remaining_tasks),
            duration_ms=turn_duration,
        ))

    # Safety net: max turns reached
    event_handler.on_max_turns(MAX_SCOUT_TURNS)
    manifest = state_to_manifest(state)
    event_handler.on_manifest(manifest)

    run_record.final_state = state.model_dump(by_alias=True)
    run_record.manifest = manifest.model_dump(by_alias=True, exclude_none=True)
    run_record.total_duration_ms = (time.monotonic() - run_start) * 1000
    run_record.termination_reason = "max_turns"
    return manifest, run_record
