"""Rich console output for Route Extractor results."""

from __future__ import annotations

import json

from rich.console import Console
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table

from swagger_agent.models import EndpointDescriptor
from swagger_agent.agents.route_extractor.harness import RouteExtractorRunRecord


def print_extraction_summary(record: RouteExtractorRunRecord, console: Console | None = None) -> None:
    console = console or Console(stderr=True)
    console.print(Rule(" Extraction Summary ", style="bold blue"))

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Target", record.target_file)
    table.add_row("Framework", record.context.get("framework", "?"))
    table.add_row("Base path", record.context.get("base_path", "") or "(none)")
    table.add_row("File lines", str(record.file_lines))
    table.add_row("Endpoints", str(record.endpoint_count))
    table.add_row("Duration", f"{record.duration_ms:.0f}ms")
    console.print(table)
    console.print()


def print_endpoints_table(descriptor: EndpointDescriptor, console: Console | None = None) -> None:
    console = console or Console(stderr=True)
    console.print(Rule(" Endpoints ", style="bold green"))

    table = Table(expand=True)
    table.add_column("Method", width=7, style="bold")
    table.add_column("Path", min_width=20)
    table.add_column("Operation ID", min_width=15)
    table.add_column("Auth", width=12)
    table.add_column("Params", width=8, justify="right")
    table.add_column("Body", width=6)
    table.add_column("Responses", min_width=12)

    for ep in descriptor.endpoints:
        method_colors = {
            "GET": "green", "POST": "yellow", "PUT": "blue",
            "PATCH": "cyan", "DELETE": "red",
        }
        method_style = method_colors.get(ep.method.upper(), "white")
        method_str = f"[{method_style}]{ep.method.upper()}[/{method_style}]"

        auth_str = ", ".join(ep.security) if ep.security else ("[dim]public[/dim]" if ep.security == [] else "[dim]-[/dim]")
        param_count = str(len(ep.parameters)) if ep.parameters else "[dim]0[/dim]"
        body_str = "[green]yes[/green]" if ep.request_body else "[dim]no[/dim]"
        responses_str = ", ".join(r.status_code for r in ep.responses)

        table.add_row(
            method_str, ep.path, ep.operation_id,
            auth_str, param_count, body_str, responses_str,
        )

    console.print(table)
    console.print()


def print_descriptor_json(descriptor: EndpointDescriptor, console: Console | None = None) -> None:
    console = console or Console(stderr=True)
    console.print(Rule(" Endpoint Descriptor (JSON) ", style="bold cyan"))
    descriptor_json = json.dumps(
        descriptor.model_dump(by_alias=True),
        indent=2, default=str,
    )
    console.print(Syntax(descriptor_json, "json", theme="monokai", line_numbers=False))
    console.print()
