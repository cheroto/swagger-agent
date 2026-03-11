"""Standalone CLI for testing the Scout agent.

Usage:
    python -m swagger_agent.agents.scout.cli /path/to/repo
    python -m swagger_agent.agents.scout.cli /path/to/repo --verbose
    python -m swagger_agent.agents.scout.cli /path/to/repo --dump-json run_output.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from rich.console import Console

from swagger_agent.config import LLMConfig
from swagger_agent.agents.scout.harness import run_scout
from swagger_agent.agents.scout.rich_handler import (
    RichScoutHandler,
    print_run_summary,
    print_manifest,
    print_final_state,
    print_all_turns,
    print_trace,
    print_turn_detail,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Scout agent against a codebase",
    )
    parser.add_argument("target_dir", help="Path to the codebase to analyze")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show full scratchpad and state updates")
    parser.add_argument("--dump-json", metavar="PATH", help="Dump full run record to JSON file")
    parser.add_argument("--show-turns", action="store_true", help="Print turn-by-turn timeline after run")
    parser.add_argument("--show-trace", action="store_true", help="Print deterministic trace after run")
    parser.add_argument("--show-state", action="store_true", help="Print final working state after run")
    parser.add_argument("--show-all", action="store_true", help="Show everything (turns, trace, state)")
    parser.add_argument("--turn", type=int, metavar="N", help="Print detailed info for turn N")
    parser.add_argument("--no-live", action="store_true", help="Disable live dashboard (use scrolling output)")
    args = parser.parse_args()

    target_dir = os.path.abspath(args.target_dir)
    if not os.path.isdir(target_dir):
        print(f"Error: {target_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    console = Console(stderr=True)
    handler = RichScoutHandler(verbose=args.verbose, console=console)
    config = LLMConfig()

    console.print(f"[dim]LLM: {config.llm_base_url} / {config.llm_model}[/dim]")
    console.print(f"[dim]Target: {target_dir}[/dim]")
    console.print()

    if not args.no_live:
        handler.start()

    try:
        manifest, record = run_scout(target_dir, config=config, event_handler=handler)
    finally:
        if not args.no_live:
            handler.stop()

    # Post-run output
    console.print()
    print_run_summary(record, console)

    if args.show_all or args.show_turns:
        print_all_turns(record, console)

    if args.show_all or args.show_trace:
        print_trace(record, console)

    if args.show_all or args.show_state:
        print_final_state(record, console)

    if args.turn:
        print_turn_detail(record, args.turn, console)

    # Always show manifest
    print_manifest(record, console)

    if args.dump_json:
        dump_dir = os.path.dirname(args.dump_json)
        if dump_dir:
            os.makedirs(dump_dir, exist_ok=True)
        dump = {
            "target_dir": record.target_dir,
            "total_duration_ms": record.total_duration_ms,
            "termination_reason": record.termination_reason,
            "turn_count": len(record.turns),
            "manifest": record.manifest,
            "final_state": record.final_state,
            "turns": [
                {
                    "turn": t.turn,
                    "scratchpad": t.scratchpad,
                    "state_updates": t.state_updates,
                    "actions": t.actions,
                    "remaining_tasks": t.remaining_tasks,
                    "duration_ms": t.duration_ms,
                }
                for t in record.turns
            ],
            "trace": {
                "tool_history": [
                    {"turn": e.turn, "tool": e.tool, "args": e.args, "summary": e.summary}
                    for e in record.trace.tool_history
                ],
                "files_touched": record.trace.files_touched,
            },
        }
        with open(args.dump_json, "w") as f:
            json.dump(dump, f, indent=2, default=str)
        console.print(f"[dim]Run record written to {args.dump_json}[/dim]")


if __name__ == "__main__":
    main()
