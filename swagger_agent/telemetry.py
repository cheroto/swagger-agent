"""Telemetry — collects per-LLM-call metrics for post-run reporting.

Thread-safe collector that accumulates LLMCall records from all agents.
Included in result.json and printed as a summary table.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class LLMCall:
    """Record of a single LLM request/response cycle."""

    agent: str  # "scout", "route_extractor", "schema_extractor"
    phase: str  # "turn_3", "phase1", "phase2", "extract"
    model: str
    input_chars: int  # sum of all message content character lengths
    output_chars: int  # serialized response character length
    input_tokens: int | None = None  # from API usage, if available
    output_tokens: int | None = None
    duration_ms: float = 0.0
    target_file: str = ""  # which file was being processed
    timestamp: float = 0.0  # time.monotonic() when call started

    def to_dict(self) -> dict:
        d = {
            "agent": self.agent,
            "phase": self.phase,
            "model": self.model,
            "input_chars": self.input_chars,
            "output_chars": self.output_chars,
            "duration_ms": round(self.duration_ms, 1),
            "target_file": self.target_file,
        }
        if self.input_tokens is not None:
            d["input_tokens"] = self.input_tokens
        if self.output_tokens is not None:
            d["output_tokens"] = self.output_tokens
        return d


class Telemetry:
    """Thread-safe collector for LLM call records."""

    def __init__(self) -> None:
        self._calls: list[LLMCall] = []
        self._lock = threading.Lock()

    def record(self, call: LLMCall) -> None:
        with self._lock:
            self._calls.append(call)

    @property
    def calls(self) -> list[LLMCall]:
        with self._lock:
            return list(self._calls)

    def summary(self) -> dict:
        """Aggregate summary for result.json."""
        calls = self.calls
        if not calls:
            return {"total_calls": 0}

        total_input = sum(c.input_chars for c in calls)
        total_output = sum(c.output_chars for c in calls)
        total_duration = sum(c.duration_ms for c in calls)

        by_agent: dict[str, dict] = {}
        for c in calls:
            if c.agent not in by_agent:
                by_agent[c.agent] = {
                    "calls": 0,
                    "input_chars": 0,
                    "output_chars": 0,
                    "duration_ms": 0.0,
                }
            by_agent[c.agent]["calls"] += 1
            by_agent[c.agent]["input_chars"] += c.input_chars
            by_agent[c.agent]["output_chars"] += c.output_chars
            by_agent[c.agent]["duration_ms"] += c.duration_ms

        # Round durations
        for v in by_agent.values():
            v["duration_ms"] = round(v["duration_ms"], 1)

        return {
            "total_calls": len(calls),
            "total_input_chars": total_input,
            "total_output_chars": total_output,
            "total_llm_duration_ms": round(total_duration, 1),
            "by_agent": by_agent,
            "calls": [c.to_dict() for c in calls],
        }


def measure_messages(messages: list[dict]) -> int:
    """Count total characters across all message contents."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += len(part.get("text", ""))
    return total
