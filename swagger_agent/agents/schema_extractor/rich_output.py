"""Rich console output for Schema Extractor results."""

from __future__ import annotations

import json

from rich.console import Console
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table

from swagger_agent.models import SchemaDescriptor
from swagger_agent.agents.schema_extractor.harness import SchemaExtractorRunRecord


def print_extraction_summary(record: SchemaExtractorRunRecord, console: Console | None = None) -> None:
    console = console or Console(stderr=True)
    console.print(Rule(" Extraction Summary ", style="bold blue"))

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Target", record.target_file)
    table.add_row("Framework", record.context.get("framework", "?"))
    table.add_row("File lines", str(record.file_lines))
    table.add_row("Schemas", str(record.schema_count))
    table.add_row("Duration", f"{record.duration_ms:.0f}ms")
    console.print(table)
    console.print()


def print_schemas_table(descriptor: SchemaDescriptor, console: Console | None = None) -> None:
    console = console or Console(stderr=True)
    console.print(Rule(" Schemas ", style="bold green"))

    table = Table(expand=True)
    table.add_column("Schema Name", min_width=20, style="bold")
    table.add_column("Properties", width=12, justify="right")
    table.add_column("Required", width=10, justify="right")
    table.add_column("Has $ref", width=10)

    for name, schema in descriptor.schemas.items():
        props = schema.get("properties", {})
        required = schema.get("required", [])
        has_ref = any(
            "$ref" in v or (isinstance(v.get("items"), dict) and "$ref" in v["items"])
            for v in props.values()
        )

        table.add_row(
            name,
            str(len(props)),
            str(len(required)),
            "[green]yes[/green]" if has_ref else "[dim]no[/dim]",
        )

    console.print(table)
    console.print()


def print_descriptor_json(descriptor: SchemaDescriptor, console: Console | None = None) -> None:
    console = console or Console(stderr=True)
    console.print(Rule(" Schema Descriptor (JSON) ", style="bold cyan"))
    descriptor_json = json.dumps(
        descriptor.model_dump(by_alias=True),
        indent=2, default=str,
    )
    console.print(Syntax(descriptor_json, "json", theme="monokai", line_numbers=False))
    console.print()
