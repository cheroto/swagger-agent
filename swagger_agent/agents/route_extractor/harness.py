"""Route Extractor agent harness — two-phase architecture.

Phase 1: Code analysis — tech-agnostic observation of routing patterns.
Phase 2: Endpoint extraction — targeted prompt built from Phase 1 output.

Externally unchanged: run_route_extractor() still returns
(EndpointDescriptor, RouteExtractorRunRecord).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from swagger_agent.config import LLMConfig, make_client
from swagger_agent.models import CodeAnalysis, EndpointDescriptor
from swagger_agent.agents.route_extractor.prompt import (
    CODE_ANALYSIS_PROMPT,
    build_phase2_prompt,
)
from swagger_agent.telemetry import LLMCall, Telemetry, measure_messages

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
    phase1_duration_ms: float = 0.0
    phase2_duration_ms: float = 0.0
    code_analysis: dict = field(default_factory=dict)
    code_analysis_obj: object = None  # CodeAnalysis Pydantic model (for test assertions)
    file_lines: int = 0


def run_route_extractor(
    target_file: str,
    context: RouteExtractorContext,
    config: LLMConfig | None = None,
    telemetry: Telemetry | None = None,
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

    # 2. Build user message (shared between both phases)
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

    # 3. Phase 1: Code Analysis
    logger.info("Phase 1: Analyzing %s (%d lines)", target_file, file_lines)
    p1_messages = [
        {"role": "system", "content": CODE_ANALYSIS_PROMPT},
        {"role": "user", "content": user_message},
    ]
    p1_input_chars = measure_messages(p1_messages)
    p1_start = time.monotonic()

    analysis = client.chat.completions.create(
        model=model,
        response_model=CodeAnalysis,
        max_retries=config.instructor_max_retries,
        messages=p1_messages,
        temperature=config.llm_temperature,
        max_tokens=4096,
    )

    phase1_duration_ms = (time.monotonic() - p1_start) * 1000
    logger.info(
        "Phase 1 complete: %d endpoints sketched, %d auth patterns, %.0fms",
        len(analysis.endpoints), len(analysis.auth_patterns), phase1_duration_ms,
    )

    if telemetry:
        p1_output = analysis.model_dump_json()
        telemetry.record(LLMCall(
            agent="route_extractor",
            phase="phase1_analysis",
            model=model,
            input_chars=p1_input_chars,
            output_chars=len(p1_output),
            duration_ms=phase1_duration_ms,
            target_file=context.target_file,
            timestamp=p1_start,
        ))

    # 4. Phase 2: Endpoint Extraction
    phase2_prompt = build_phase2_prompt(analysis, context.base_path)

    logger.info("Phase 2: Extracting endpoints from %s", target_file)
    p2_messages = [
        {"role": "system", "content": phase2_prompt},
        {"role": "user", "content": user_message},
    ]
    p2_input_chars = measure_messages(p2_messages)
    p2_start = time.monotonic()

    descriptor = client.chat.completions.create(
        model=model,
        response_model=EndpointDescriptor,
        max_retries=config.instructor_max_retries,
        messages=p2_messages,
        temperature=config.llm_temperature,
        max_tokens=config.llm_max_tokens,
    )

    phase2_duration_ms = (time.monotonic() - p2_start) * 1000
    total_duration_ms = phase1_duration_ms + phase2_duration_ms

    if telemetry:
        p2_output = descriptor.model_dump_json()
        telemetry.record(LLMCall(
            agent="route_extractor",
            phase="phase2_extraction",
            model=model,
            input_chars=p2_input_chars,
            output_chars=len(p2_output),
            duration_ms=phase2_duration_ms,
            target_file=context.target_file,
            timestamp=p2_start,
        ))

    # 5. Inject source_file (don't trust LLM to get the path right)
    descriptor.source_file = context.target_file

    logger.info(
        "Phase 2 complete: %d endpoints from %s in %.0fms (total %.0fms)",
        len(descriptor.endpoints), target_file, phase2_duration_ms, total_duration_ms,
    )

    # 6. Build run record
    run_record = RouteExtractorRunRecord(
        target_file=context.target_file,
        context={
            "framework": context.framework,
            "base_path": context.base_path,
            "target_file": context.target_file,
        },
        endpoint_count=len(descriptor.endpoints),
        descriptor=descriptor.model_dump(by_alias=True),
        duration_ms=total_duration_ms,
        phase1_duration_ms=phase1_duration_ms,
        phase2_duration_ms=phase2_duration_ms,
        code_analysis=analysis.model_dump(),
        code_analysis_obj=analysis,
        file_lines=file_lines,
    )

    return descriptor, run_record
