"""Standalone CLI for testing the Schema Extractor agent.

Usage:
    # With explicit context
    python -m swagger_agent.agents.schema_extractor.cli path/to/model.py \\
      --framework fastapi

    # With known schemas from file
    python -m swagger_agent.agents.schema_extractor.cli path/to/model.py \\
      --framework spring --known-schemas deps.json

    # Dump output
    python -m swagger_agent.agents.schema_extractor.cli path/to/model.py \\
      --framework fastapi --dump-json output.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from rich.console import Console

from swagger_agent.config import LLMConfig
from swagger_agent.agents.schema_extractor.harness import (
    run_schema_extractor,
    SchemaExtractorContext,
)
from swagger_agent.agents.schema_extractor.rich_output import (
    print_extraction_summary,
    print_schemas_table,
    print_descriptor_json,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Schema Extractor agent on a single model file",
    )
    parser.add_argument("target_file", help="Path to the model file to extract")
    parser.add_argument("--framework", required=True, help="Framework name")
    parser.add_argument(
        "--known-schemas", metavar="PATH",
        help="JSON file with dependency schemas: {\"SchemaName\": {...}, ...}",
    )
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

    # Load known schemas if provided
    known_schemas: dict[str, dict] = {}
    if args.known_schemas:
        if not os.path.isfile(args.known_schemas):
            print(f"Error: known schemas file not found: {args.known_schemas}", file=sys.stderr)
            sys.exit(1)
        with open(args.known_schemas) as f:
            known_schemas = json.load(f)

    context = SchemaExtractorContext(
        framework=args.framework,
        target_file=target_file,
        known_schemas=known_schemas,
    )

    console = Console(stderr=True)
    config = LLMConfig()

    console.print(f"[dim]LLM: {config.llm_base_url} / {config.llm_model}[/dim]")
    console.print(f"[dim]Target: {target_file}[/dim]")
    console.print(f"[dim]Framework: {args.framework}[/dim]")
    if known_schemas:
        console.print(f"[dim]Known schemas: {', '.join(known_schemas.keys())}[/dim]")
    console.print()

    descriptor, record = run_schema_extractor(target_file, context, config=config)

    # Post-run output
    print_extraction_summary(record, console)
    print_schemas_table(descriptor, console)

    if args.verbose:
        print_descriptor_json(descriptor, console)

    if args.dump_json:
        dump_dir = os.path.dirname(args.dump_json)
        if dump_dir:
            os.makedirs(dump_dir, exist_ok=True)
        dump = {
            "target_file": record.target_file,
            "context": record.context,
            "schema_count": record.schema_count,
            "file_lines": record.file_lines,
            "duration_ms": record.duration_ms,
            "descriptor": record.descriptor,
        }
        with open(args.dump_json, "w") as f:
            json.dump(dump, f, indent=2, default=str)
        console.print(f"[dim]Run record written to {args.dump_json}[/dim]")


if __name__ == "__main__":
    main()
