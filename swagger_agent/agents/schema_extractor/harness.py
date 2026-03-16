"""Schema Extractor agent harness — single-call architecture.

Like the Route Extractor, this is a single instructor call: harness reads
the file, passes content + known_schemas to LLM, gets back a structured
SchemaDescriptor.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from swagger_agent.config import LLMConfig, make_client
from swagger_agent.models import SchemaDescriptor
from swagger_agent.agents.schema_extractor.prompt import SCHEMA_EXTRACTOR_SYSTEM_PROMPT
from swagger_agent.telemetry import LLMCall, Telemetry, measure_messages

logger = logging.getLogger("swagger_agent.schema_extractor")


@dataclass
class SchemaExtractorContext:
    framework: str
    target_file: str
    known_schemas: dict[str, dict] = field(default_factory=dict)


@dataclass
class SchemaExtractorRunRecord:
    target_file: str = ""
    context: dict = field(default_factory=dict)
    schema_count: int = 0
    descriptor: dict = field(default_factory=dict)
    duration_ms: float = 0.0
    file_lines: int = 0


def run_schema_extractor(
    target_file: str,
    context: SchemaExtractorContext,
    config: LLMConfig | None = None,
    telemetry: Telemetry | None = None,
) -> tuple[SchemaDescriptor, SchemaExtractorRunRecord]:
    """Run the Schema Extractor agent against a single model file.

    Returns (descriptor, run_record). The harness reads the file and sets
    source_file on the descriptor — the LLM is not trusted for file paths.
    """
    if config is None:
        config = LLMConfig()

    client, model = make_client(config, "schema_extractor")

    # 1. Read the target file (infrastructure responsibility)
    file_path = Path(target_file)
    if not file_path.is_file():
        raise FileNotFoundError(f"Model file not found: {target_file}")

    file_content = file_path.read_text(encoding="utf-8", errors="replace")
    file_lines = file_content.count("\n") + 1

    # 2. Build messages
    context_json = json.dumps({
        "framework": context.framework,
        "target_file": context.target_file,
        "known_schemas": context.known_schemas,
    }, indent=2)

    user_message = (
        f"## Context\n\n```json\n{context_json}\n```\n\n"
        f"## Model File: {context.target_file}\n\n"
        f"```\n{file_content}\n```"
    )

    messages = [
        {"role": "system", "content": SCHEMA_EXTRACTOR_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    # 3. Single instructor call
    logger.info("Extracting schemas from %s (%d lines)", target_file, file_lines)
    input_chars = measure_messages(messages)
    start = time.monotonic()

    descriptor = client.chat.completions.create(
        model=model,
        response_model=SchemaDescriptor,
        max_retries=config.instructor_max_retries,
        messages=messages,
        temperature=config.llm_temperature,
        max_tokens=config.llm_max_tokens,
        **config.extra_create_kwargs(),
    )

    duration_ms = (time.monotonic() - start) * 1000

    if telemetry:
        output_json = descriptor.model_dump_json()
        telemetry.record(LLMCall(
            agent="schema_extractor",
            phase="extract",
            model=model,
            input_chars=input_chars,
            output_chars=len(output_json),
            duration_ms=duration_ms,
            target_file=context.target_file,
            timestamp=start,
        ))

    # 4. Inject source_file (don't trust LLM to get the path right)
    descriptor.source_file = context.target_file

    logger.info(
        "Extracted %d schemas from %s in %.0fms",
        len(descriptor.schemas), target_file, duration_ms,
    )

    # 5. Build run record
    run_record = SchemaExtractorRunRecord(
        target_file=context.target_file,
        context={
            "framework": context.framework,
            "target_file": context.target_file,
            "known_schemas_keys": list(context.known_schemas.keys()),
        },
        schema_count=len(descriptor.schemas),
        descriptor=descriptor.model_dump(by_alias=True),
        duration_ms=duration_ms,
        file_lines=file_lines,
    )

    return descriptor, run_record
