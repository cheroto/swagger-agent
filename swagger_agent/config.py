from __future__ import annotations

import instructor
from openai import OpenAI
from pydantic_settings import BaseSettings


class LLMConfig(BaseSettings):
    model_config = {"env_prefix": "", "env_file": ".env"}

    # Defaults
    llm_base_url: str = "http://server-pedro.local:8080/v1"
    llm_model: str = "qwen35-35b-a3b-instruct"
    llm_api_key: str = "not-needed"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 16384
    instructor_max_retries: int = 3

    # Per-agent overrides
    llm_model_scout: str = ""
    llm_model_route_extractor: str = ""
    llm_model_schema_extractor: str = ""
    llm_model_orchestrator: str = ""

    llm_base_url_scout: str = ""
    llm_base_url_route_extractor: str = ""
    llm_base_url_schema_extractor: str = ""
    llm_base_url_orchestrator: str = ""

    def for_agent(self, agent: str) -> tuple[str, str]:
        """Return (base_url, model) for a given agent, falling back to defaults."""
        agent_key = agent.lower().replace(" ", "_")
        base_url = getattr(self, f"llm_base_url_{agent_key}", "") or self.llm_base_url
        model = getattr(self, f"llm_model_{agent_key}", "") or self.llm_model
        return base_url, model


def make_client(config: LLMConfig, agent: str) -> tuple[instructor.Instructor, str]:
    """Create an instructor-patched OpenAI client for a specific agent."""
    base_url, model = config.for_agent(agent)
    raw_client = OpenAI(base_url=base_url, api_key=config.llm_api_key)
    return instructor.from_openai(raw_client), model


def make_raw_client(config: LLMConfig, agent: str) -> tuple[OpenAI, str]:
    """Create a raw OpenAI client (no instructor) for tool-calling agents."""
    base_url, model = config.for_agent(agent)
    raw_client = OpenAI(base_url=base_url, api_key=config.llm_api_key)
    return raw_client, model
