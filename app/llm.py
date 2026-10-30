"""LLM client for calling the configured model endpoint (OpenAI-compatible API)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI

from .config import get_settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when the LLM call fails."""


class LLMNotConfiguredError(LLMError):
    """Raised when LLM_BASE_URL or LLM_MODEL is not configured."""


def _get_client() -> AsyncOpenAI:
    """Create an AsyncOpenAI client from current settings."""
    settings = get_settings()
    if not settings.llm_base_url:
        raise LLMNotConfiguredError(
            "LLM_BASE_URL is not set. Configure it in .env to use AI agents."
        )
    if not settings.llm_model:
        raise LLMNotConfiguredError(
            "LLM_MODEL is not set. Configure it in .env to use AI agents."
        )
    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "none",
        timeout=float(settings.llm_timeout_seconds),
    )


def _extract_json_text(raw: str) -> str:
    """Extract and clean JSON payload from raw LLM output."""
    # 1. Strip <think>...</think> blocks
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    # 2. Extract from code block if present
    code_block = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
    if code_block:
        candidate = code_block.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            pass

    # 3. Direct parse test
    try:
        json.loads(text)
        return text
    except Exception:
        pass

    # 4. Search for outermost { ... } or [ ... ]
    first_brace = text.find("{")
    first_bracket = text.find("[")

    if first_brace != -1 and (first_bracket == -1 or first_brace < first_bracket):
        last_brace = text.rfind("}")
        if last_brace > first_brace:
            candidate = text[first_brace : last_brace + 1].strip()
            try:
                json.loads(candidate)
                return candidate
            except Exception:
                pass

    if first_bracket != -1:
        last_bracket = text.rfind("]")
        if last_bracket > first_bracket:
            candidate = text[first_bracket : last_bracket + 1].strip()
            try:
                json.loads(candidate)
                return candidate
            except Exception:
                pass

    return text


async def chat(
    messages: list[dict[str, str]],
    temperature: float = 0.3,
    model: str | None = None,
) -> str:
    """
    Call the LLM with the given messages and return the response text.

    Args:
        messages: List of {role, content} dicts (system/user/assistant).
        temperature: Sampling temperature (0.0 = deterministic).
        model: Override model name; defaults to settings.llm_model.

    Returns:
        The assistant's text response.

    Raises:
        LLMNotConfiguredError: If LLM_BASE_URL or LLM_MODEL are not set.
        LLMError: On any API error.
    """
    settings = get_settings()
    client = _get_client()
    effective_model = model or settings.llm_model

    try:
        response = await client.chat.completions.create(
            model=effective_model,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
        )
        content = response.choices[0].message.content
        return content or ""
    except LLMNotConfiguredError:
        raise
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        raise LLMError(f"LLM call failed: {exc}") from exc


async def chat_json(
    messages: list[dict[str, str]],
    temperature: float = 0.1,
    model: str | None = None,
) -> Any:
    """
    Call the LLM and parse the response as JSON.

    Strips <think> blocks, markdown fences, and extracts JSON objects/arrays.

    Returns:
        Parsed JSON object (dict, list, etc.)

    Raises:
        LLMError: If the response cannot be parsed as JSON.
    """
    raw = await chat(messages, temperature=temperature, model=model)
    text = _extract_json_text(raw)

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("LLM returned non-JSON: %s", raw[:500])
        raise LLMError(f"LLM response is not valid JSON: {exc}") from exc
