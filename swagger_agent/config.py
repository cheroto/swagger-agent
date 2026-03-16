from __future__ import annotations

import instructor
from openai import OpenAI
from pydantic_settings import BaseSettings


# Map string names to instructor.Mode values for .env configuration
_INSTRUCTOR_MODES: dict[str, instructor.Mode] = {
    "tools": instructor.Mode.TOOLS,
    "json": instructor.Mode.JSON,
    "json_schema": instructor.Mode.JSON_SCHEMA,
    "md_json": instructor.Mode.MD_JSON,
    "openrouter_structured_outputs": instructor.Mode.OPENROUTER_STRUCTURED_OUTPUTS,
}


class LLMConfig(BaseSettings):
    model_config = {"env_prefix": "", "env_file": ".env"}

    # Defaults
    llm_base_url: str = "http://localhost:8080/v1"
    llm_model: str = "default"
    llm_api_key: str = "not-needed"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 16384
    instructor_max_retries: int = 3
    instructor_mode: str = "tools"

    # Reasoning effort: "", "none", "low", "medium", "high"
    # Works with OpenAI o-series and Google Gemini 2.5+ thinking models.
    # Empty string = don't set (use model default).
    llm_reasoning_effort: str = ""

    # Per-agent overrides
    llm_model_scout: str = ""
    llm_model_route_extractor: str = ""
    llm_model_schema_extractor: str = ""
    llm_model_orchestrator: str = ""

    llm_base_url_scout: str = ""
    llm_base_url_route_extractor: str = ""
    llm_base_url_schema_extractor: str = ""
    llm_base_url_orchestrator: str = ""

    # Concurrency settings (1 = sequential, current behavior)
    max_workers_route: int = 1
    max_workers_schema: int = 1

    def extra_create_kwargs(self) -> dict[str, str]:
        """Return extra kwargs to pass to chat.completions.create().

        Currently handles reasoning_effort for thinking models
        (OpenAI o-series, Google Gemini 2.5+).
        """
        kwargs: dict[str, str] = {}
        if self.llm_reasoning_effort:
            kwargs["reasoning_effort"] = self.llm_reasoning_effort
        return kwargs

    def for_agent(self, agent: str) -> tuple[str, str]:
        """Return (base_url, model) for a given agent, falling back to defaults."""
        agent_key = agent.lower().replace(" ", "_")
        base_url = getattr(self, f"llm_base_url_{agent_key}", "") or self.llm_base_url
        model = getattr(self, f"llm_model_{agent_key}", "") or self.llm_model
        return base_url, model


# Module-level cache mode — set by CLI before any clients are created
# "on" = read+write (default), "off" = no cache, "overwrite" = skip reads, write fresh
_cache_mode: str = "on"


def set_cache_mode(mode: str) -> None:
    global _cache_mode
    _cache_mode = mode


def make_client(config: LLMConfig, agent: str) -> tuple[instructor.Instructor, str]:
    """Create an instructor-patched OpenAI client for a specific agent."""
    base_url, model = config.for_agent(agent)
    raw_client = OpenAI(base_url=base_url, api_key=config.llm_api_key)
    mode = _INSTRUCTOR_MODES.get(config.instructor_mode.lower(), instructor.Mode.TOOLS)
    client = instructor.from_openai(raw_client, mode=mode)
    if _cache_mode != "off":
        from swagger_agent.cache import wrap_client
        client = wrap_client(client, base_url, overwrite=(_cache_mode == "overwrite"))
    return client, model


def make_raw_client(config: LLMConfig, agent: str) -> tuple[OpenAI, str]:
    """Create a raw OpenAI client (no instructor) for tool-calling agents."""
    base_url, model = config.for_agent(agent)
    raw_client = OpenAI(base_url=base_url, api_key=config.llm_api_key)
    return raw_client, model
