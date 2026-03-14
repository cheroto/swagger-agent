"""Standalone CLI for testing the Route Extractor agent.

Usage:
    # With explicit context
    python -m swagger_agent.agents.route_extractor.cli path/to/route_file.py \\
      --framework fastapi --base-path /api

    # With manifest from Scout
    python -m swagger_agent.agents.route_extractor.cli path/to/route_file.py \\
      --manifest outputs/dump1/manifest.json

    # Dump output
    python -m swagger_agent.agents.route_extractor.cli path/to/route_file.py \\
      --framework express --dump-json output.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from rich.console import Console

from swagger_agent.config import LLMConfig
from swagger_agent.agents.route_extractor.harness import (
    run_route_extractor,
    RouteExtractorContext,
)
from swagger_agent.agents.route_extractor.rich_output import (
    print_extraction_summary,
    print_endpoints_table,
    print_descriptor_json,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Route Extractor agent on a single route file",
    )
    parser.add_argument("target_file", help="Path to the route file to extract")
    parser.add_argument("--framework", help="Framework name (required unless --manifest)")
    parser.add_argument("--base-path", default="", help="API base path (default: '')")
    parser.add_argument("--manifest", metavar="PATH", help="Path to a discovery manifest JSON")
    parser.add_argument("--dump-json", metavar="PATH", help="Save full run record to JSON file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show full JSON descriptor")
    parser.add_argument("--cache", action="store_true", help="Enable LLM response caching")
    args = parser.parse_args()

    if args.cache:
        from swagger_agent.config import enable_cache
        enable_cache()

    target_file = os.path.abspath(args.target_file)
    if not os.path.isfile(target_file):
        print(f"Error: {target_file} is not a file", file=sys.stderr)
        sys.exit(1)

    # Resolve framework and base_path
    framework = args.framework
    base_path = args.base_path

    if args.manifest:
        if not os.path.isfile(args.manifest):
            print(f"Error: manifest not found: {args.manifest}", file=sys.stderr)
            sys.exit(1)
        with open(args.manifest) as f:
            manifest = json.load(f)
        framework = framework or manifest.get("framework", "")
        base_path = base_path or manifest.get("base_path", "")

    if not framework:
        print("Error: --framework is required (or provide --manifest)", file=sys.stderr)
        sys.exit(1)

    context = RouteExtractorContext(
        framework=framework,
        base_path=base_path,
        target_file=target_file,
    )

    console = Console(stderr=True)
    config = LLMConfig()

    console.print(f"[dim]LLM: {config.llm_base_url} / {config.llm_model}[/dim]")
    console.print(f"[dim]Target: {target_file}[/dim]")
    console.print(f"[dim]Framework: {framework}, Base path: {base_path or '(none)'}[/dim]")
    console.print()

    descriptor, record = run_route_extractor(target_file, context, config=config)

    # Post-run output
    print_extraction_summary(record, console)
    print_endpoints_table(descriptor, console)

    if args.verbose:
        print_descriptor_json(descriptor, console)

    if args.dump_json:
        dump_dir = os.path.dirname(args.dump_json)
        if dump_dir:
            os.makedirs(dump_dir, exist_ok=True)
        dump = {
            "target_file": record.target_file,
            "context": record.context,
            "endpoint_count": record.endpoint_count,
            "file_lines": record.file_lines,
            "duration_ms": record.duration_ms,
            "descriptor": record.descriptor,
        }
        with open(args.dump_json, "w") as f:
            json.dump(dump, f, indent=2, default=str)
        console.print(f"[dim]Run record written to {args.dump_json}[/dim]")


if __name__ == "__main__":
    main()
