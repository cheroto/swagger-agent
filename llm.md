# LLM & Agent Framework

## Stack Decision

**`openai` SDK + `instructor` + plain Python.** No agent framework.

### Why not LangGraph

LangGraph's value is complex branching state machines with checkpointing, parallel fan-out, and human-in-the-loop. This project has none of that:

- The Scout is a standard ReAct loop (~40 lines of Python).
- Route Extractor and Schema Extractor are single structured LLM calls.
- The Orchestrator is a while loop that reads state and picks an action.
- All complex flow control (ref resolution, schema extraction queue, assembly pipeline) is deterministic infrastructure code that LangGraph can't help with.

LangGraph would sit between infrastructure and agents, forcing artifact flow through its state management instead of through the artifact store. That fights the architecture in CLAUDE.md where infrastructure owns the pipeline and agents are isolated workers.

### Why not PydanticAI / Mirascope / smolagents

Same reasoning. These frameworks wrap the LLM call with agent abstractions. But the only agent that needs a multi-step loop is the Scout, and a hand-rolled ReAct loop gives full control over state injection (the working state that gets re-injected every step). The extractors are single calls. Wrapping them in an agent framework adds indirection for no payoff.

### Why instructor

`instructor` patches the OpenAI client to return Pydantic models directly. This is the structured output layer:

- Route Extractor returns `EndpointDescriptor` — instructor validates and retries on schema violations.
- Schema Extractor returns `SchemaDescriptor` — same.
- Scout tool calls use OpenAI function calling natively; `instructor` handles the final `DiscoveryManifest` output.
- Orchestrator decisions are structured (`delegate` or `mark_complete`) — instructor enforces this.

Instructor's retry logic (with `max_retries`) handles malformed JSON from smaller models like Qwen without custom retry code.

## LLM Client Configuration

All configuration via `.env` file. Every value has a sensible default.

### Environment Variables

```bash
# .env

# --- Primary LLM endpoint ---
LLM_BASE_URL=http://localhost:8080/v1
LLM_MODEL=your-model-name
LLM_API_KEY=not-needed                    # local servers don't require a key, but openai SDK requires a non-empty string

# --- Per-agent model overrides (optional) ---
# Use these to assign different models to different agents.
# If unset, each agent uses LLM_MODEL.
LLM_MODEL_SCOUT=
LLM_MODEL_ROUTE_EXTRACTOR=
LLM_MODEL_SCHEMA_EXTRACTOR=
LLM_MODEL_ORCHESTRATOR=

# --- Per-agent endpoint overrides (optional) ---
# Use these to point specific agents at different endpoints.
# If unset, each agent uses LLM_BASE_URL.
LLM_BASE_URL_SCOUT=
LLM_BASE_URL_ROUTE_EXTRACTOR=
LLM_BASE_URL_SCHEMA_EXTRACTOR=
LLM_BASE_URL_ORCHESTRATOR=

# --- Generation parameters ---
LLM_TEMPERATURE=0.2                       # Low for structured extraction
LLM_MAX_TOKENS=16384                      # Qwen 3.5 supports up to 32k output

# --- Instructor settings ---
INSTRUCTOR_MAX_RETRIES=3                  # Retries on schema validation failure

# --- Optional: switch to cloud provider ---
# Uncomment to use OpenRouter, Together, Groq, or any OpenAI-compatible API:
# LLM_BASE_URL=https://openrouter.ai/api/v1
# LLM_MODEL=your-model-name
# LLM_API_KEY=sk-or-...
```

### Config Model

```python
from pydantic_settings import BaseSettings

class LLMConfig(BaseSettings):
    model_config = {"env_prefix": "", "env_file": ".env"}

    # Defaults
    llm_base_url: str = "http://localhost:8080/v1"
    llm_model: str = "default"
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
```

### Client Factory

```python
import instructor
from openai import OpenAI

def make_client(config: LLMConfig, agent: str) -> instructor.Instructor:
    """Create an instructor-patched OpenAI client for a specific agent."""
    base_url, model = config.for_agent(agent)
    raw_client = OpenAI(base_url=base_url, api_key=config.llm_api_key)
    return instructor.from_openai(raw_client), model
```

Usage in infrastructure:

```python
config = LLMConfig()
client, model = make_client(config, "route_extractor")

descriptor = client.chat.completions.create(
    model=model,
    response_model=EndpointDescriptor,
    max_retries=config.instructor_max_retries,
    messages=[
        {"role": "system", "content": ROUTE_EXTRACTOR_PROMPT},
        {"role": "user", "content": route_file_content},
    ],
    temperature=config.llm_temperature,
    max_tokens=config.llm_max_tokens,
)
# descriptor is a validated EndpointDescriptor instance
```

## Agent Implementation Patterns

### Single-call agents (Route Extractor, Schema Extractor)

These are just instructor calls. No loop, no tools, no framework.

```python
def run_route_extractor(
    file_content: str,
    context: RouteExtractorContext,
    client: instructor.Instructor,
    model: str,
    config: LLMConfig,
) -> EndpointDescriptor:
    return client.chat.completions.create(
        model=model,
        response_model=EndpointDescriptor,
        max_retries=config.instructor_max_retries,
        messages=[
            {"role": "system", "content": build_route_extractor_prompt(context)},
            {"role": "user", "content": file_content},
        ],
        temperature=config.llm_temperature,
        max_tokens=config.llm_max_tokens,
    )
```

### ReAct agent (Scout)

A while loop with tool dispatch. Working state is re-injected every iteration.

```python
def run_scout(target_dir: str, client: OpenAI, model: str, config: LLMConfig) -> DiscoveryManifest:
    state = ScoutWorkingState()
    tools = build_scout_tools(target_dir)     # glob, grep, read_file_head, read_file_range, update_state, write_artifact
    tool_schemas = [t.schema for t in tools]  # OpenAI function schemas
    messages = [{"role": "system", "content": SCOUT_SYSTEM_PROMPT}]

    for step in range(MAX_SCOUT_STEPS):
        # Re-inject current state at every step
        messages.append({
            "role": "user",
            "content": f"Current state:\n{state.model_dump_json(indent=2)}"
        })

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tool_schemas,
            temperature=config.llm_temperature,
        )

        message = response.choices[0].message
        messages.append(message)

        if not message.tool_calls:
            break  # Agent decided it's done

        for tool_call in message.tool_calls:
            result = tools[tool_call.function.name].execute(
                **json.loads(tool_call.function.arguments)
            )

            # Intercept state updates
            if tool_call.function.name == "update_state":
                state = apply_state_update(state, result)

            # Intercept write_artifact to capture final output
            if tool_call.function.name == "write_artifact":
                return DiscoveryManifest.model_validate(result)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result),
            })

    # Fallback: serialize whatever state we have
    return state_to_manifest(state)
```

Key design choices:
- **State re-injection**: the `Current state:` message is appended every step so the LLM always sees accumulated findings, even as earlier messages scroll out of effective context.
- **No instructor for the loop**: the Scout uses raw OpenAI function calling for tool dispatch. Instructor is only used if you want to validate the final manifest output.
- **Step limit**: `MAX_SCOUT_STEPS` prevents runaway loops. 30-50 steps is reasonable for most codebases.

### Orchestrator

A decision loop, not a ReAct agent. It reads a state summary and picks one action.

```python
class OrchestratorAction(BaseModel):
    action: Literal["delegate", "mark_complete"]
    agent: Literal["scout", "route_extractor"] | None = None
    target_file: str | None = None
    reason: str = ""

def run_orchestrator_step(
    state: StateSummary,
    client: instructor.Instructor,
    model: str,
    config: LLMConfig,
) -> OrchestratorAction:
    return client.chat.completions.create(
        model=model,
        response_model=OrchestratorAction,
        max_retries=config.instructor_max_retries,
        messages=[
            {"role": "system", "content": ORCHESTRATOR_SYSTEM_PROMPT},
            {"role": "user", "content": state.model_dump_json()},
        ],
        temperature=0.0,  # Deterministic decisions
    )
```

The orchestrator loop in infrastructure calls `run_orchestrator_step` repeatedly, executes the returned action, updates state, and loops until `mark_complete` or max retries.

## Tool Definition Pattern

Scout tools are defined as simple dataclasses with an OpenAI function schema and an execute method:

```python
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict          # JSON Schema for parameters
    fn: Callable[..., Any]    # Implementation

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def execute(self, **kwargs) -> Any:
        return self.fn(**kwargs)
```

No framework needed. The tool schemas are passed to the OpenAI API, and tool execution is a dict lookup + function call.

## Small Model Considerations

Things to account for when using smaller models (e.g. Qwen 3.5, Llama, etc.):

- **Thinking mode**: Some models have a thinking/reasoning mode that produces `<think>` tags. For structured extraction, disable thinking mode or strip thinking tags from output. Instructor should handle this if the final output validates.
- **Tool calling format**: Verify your serving backend has the chat template set correctly for function calling.
- **Context window**: Route files and model files should fit easily in 32k context. For the Scout, the re-injected state needs monitoring — the step limit prevents blowout.
- **JSON adherence**: Smaller models sometimes produce malformed JSON. Instructor's `max_retries` handles this — 3 retries is usually sufficient. If a specific agent consistently fails, increase retries or simplify the response model for that agent.
- **Temperature**: 0.2 for extraction (low creativity, high faithfulness to source code). 0.0 for orchestrator decisions.

## Switching Providers

To switch to a cloud provider, only `.env` changes:

```bash
# OpenRouter
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=your-model-name
LLM_API_KEY=sk-or-...

# Together
LLM_BASE_URL=https://api.together.xyz/v1
LLM_MODEL=your-model-name
LLM_API_KEY=...

# Groq
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=your-model-name
LLM_API_KEY=gsk_...

# Local Ollama
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=your-model-name
LLM_API_KEY=ollama
```

No code changes. The OpenAI client + instructor work with any OpenAI-compatible endpoint.

## Dependencies

```
openai>=1.40
instructor>=1.7
pydantic>=2.0
pydantic-settings>=2.0
python-dotenv>=1.0
```

## Summary

| Component | Implementation |
|-----------|---------------|
| LLM client | `openai.OpenAI` with configurable base_url |
| Structured output | `instructor.from_openai()` wrapping the client |
| Single-call agents | One `client.chat.completions.create()` with `response_model` |
| Scout (ReAct) | Hand-rolled while loop with tool dispatch + state re-injection |
| Orchestrator | Instructor call returning `OrchestratorAction`, called in infra loop |
| Tool definitions | Dataclass with name, JSON Schema params, and execute function |
| Config | `pydantic-settings` loading from `.env` with per-agent overrides |
| Framework | None. Plain Python. |
