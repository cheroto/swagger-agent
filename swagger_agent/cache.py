"""LLM response cache for deterministic replay during testing.

Caches instructor calls keyed on (base_url, model, temperature, messages, response_model).
Prompt changes (including schema descriptions in system prompts) automatically invalidate
the cache entry since the full message content is hashed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CACHE_DIR = Path(".cache/llm")


def _cache_key(
    model: str,
    temperature: float,
    base_url: str,
    messages: list[dict[str, str]],
    response_model_name: str,
    response_model_schema: str = "",
    reasoning_effort: str = "",
) -> str:
    """SHA-256 hash of all inputs that affect LLM output.

    Includes the response model's JSON schema so that field description
    changes (which instructor passes to the LLM as tool definitions)
    invalidate the cache automatically.
    """
    key_data: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "base_url": base_url,
        "messages": messages,
        "response_model": response_model_name,
        "response_model_schema": response_model_schema,
    }
    if reasoning_effort:
        key_data["reasoning_effort"] = reasoning_effort
    blob = json.dumps(key_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def load(key: str) -> dict[str, Any] | None:
    """Load a cached response, or None on miss."""
    p = cache_path(key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupt cache entry %s, ignoring", p.name)
        return None


def store(key: str, data: dict[str, Any]) -> None:
    """Write a response to the cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path(key).write_text(json.dumps(data, indent=2, default=str))


def clear() -> int:
    """Remove all cached entries. Returns count of files removed."""
    if not CACHE_DIR.exists():
        return 0
    count = 0
    for f in CACHE_DIR.glob("*.json"):
        f.unlink()
        count += 1
    return count


def wrap_client(client: Any, base_url: str, *, overwrite: bool = False) -> Any:
    """Wrap an instructor client so that chat.completions.create() is cached.

    The wrapper intercepts calls, checks the cache, and on miss delegates to
    the real client, caches the result, then returns it.

    When ``overwrite=True``, existing cache entries are ignored (always calls
    the LLM) and the result overwrites any previous entry.
    """
    real_create = client.chat.completions.create

    def cached_create(
        *,
        model: str,
        response_model: type,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        reasoning_effort: str = "",
        **kwargs: Any,
    ) -> Any:
        # Include the model's JSON schema in the cache key so that
        # field description changes invalidate cached responses.
        schema_str = json.dumps(response_model.model_json_schema(), sort_keys=True)
        key = _cache_key(model, temperature, base_url, messages, response_model.__name__, schema_str, reasoning_effort)

        # Pass reasoning_effort through to real create if set
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort

        if not overwrite:
            cached = load(key)
            if cached is not None:
                logger.info("Cache HIT: %s (model=%s)", response_model.__name__, model)
                return response_model.model_validate(cached)

        logger.info("Cache %s: %s (model=%s)",
                     "OVERWRITE" if overwrite else "MISS",
                     response_model.__name__, model)
        result = real_create(
            model=model,
            response_model=response_model,
            messages=messages,
            temperature=temperature,
            **kwargs,
        )
        store(key, result.model_dump(mode="json", by_alias=True))
        return result

    client.chat.completions.create = cached_create
    return client
