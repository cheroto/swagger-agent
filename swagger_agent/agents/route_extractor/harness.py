"""Route Extractor agent harness — single-call architecture.

Unlike the Scout (multi-turn ReAct loop), the Route Extractor is a single
instructor call: harness reads the file, passes content to LLM, gets back
a structured EndpointDescriptor.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from swagger_agent.config import LLMConfig, make_client
from swagger_agent.models import EndpointDescriptor
from swagger_agent.agents.route_extractor.prompt import ROUTE_EXTRACTOR_SYSTEM_PROMPT

logger = logging.getLogger("swagger_agent.route_extractor")


@dataclass
class RouteExtractorContext:
    framework: str
    base_path: str
    target_file: str


@dataclass
class RouteExtractorRunRecord:
    target_file: str = ""
    context: dict = field(default_factory=dict)
    endpoint_count: int = 0
    descriptor: dict = field(default_factory=dict)
    duration_ms: float = 0.0
    file_lines: int = 0


def run_route_extractor(
    target_file: str,
    context: RouteExtractorContext,
    config: LLMConfig | None = None,
) -> tuple[EndpointDescriptor, RouteExtractorRunRecord]:
    """Run the Route Extractor agent against a single route file.

    Returns (descriptor, run_record). The harness reads the file and sets
    source_file on the descriptor — the LLM is not trusted for file paths.
    """
    if config is None:
        config = LLMConfig()

    client, model = make_client(config, "route_extractor")

    # 1. Read the target file (infrastructure responsibility)
    file_path = Path(target_file)
    if not file_path.is_file():
        raise FileNotFoundError(f"Route file not found: {target_file}")

    file_content = file_path.read_text(encoding="utf-8", errors="replace")
    file_lines = file_content.count("\n") + 1

    # 2. Build messages
    context_json = json.dumps({
        "framework": context.framework,
        "base_path": context.base_path,
        "target_file": context.target_file,
    }, indent=2)

    user_message = (
        f"## Context\n\n```json\n{context_json}\n```\n\n"
        f"## Route File: {context.target_file}\n\n"
        f"```\n{file_content}\n```"
    )

    messages = [
        {"role": "system", "content": ROUTE_EXTRACTOR_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    # 3. Single instructor call
    logger.info("Extracting routes from %s (%d lines)", target_file, file_lines)
    start = time.monotonic()

    descriptor = client.chat.completions.create(
        model=model,
        response_model=EndpointDescriptor,
        max_retries=config.instructor_max_retries,
        messages=messages,
        temperature=config.llm_temperature,
        max_tokens=config.llm_max_tokens,
    )

    duration_ms = (time.monotonic() - start) * 1000

    # 4. Inject source_file (don't trust LLM to get the path right)
    descriptor.source_file = context.target_file

    logger.info(
        "Extracted %d endpoints from %s in %.0fms",
        len(descriptor.endpoints), target_file, duration_ms,
    )

    # 5. Build run record
    run_record = RouteExtractorRunRecord(
        target_file=context.target_file,
        context={
            "framework": context.framework,
            "base_path": context.base_path,
            "target_file": context.target_file,
        },
        endpoint_count=len(descriptor.endpoints),
        descriptor=descriptor.model_dump(by_alias=True),
        duration_ms=duration_ms,
        file_lines=file_lines,
    )

    return descriptor, run_record
