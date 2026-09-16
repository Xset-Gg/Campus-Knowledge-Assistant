"""LLM provider adapters.

The generation chain depends on the narrow `LLMClient` protocol below rather
than on a specific vendor SDK, so swapping providers is a config change. The
Anthropic adapter is the default and the one tuned for this workload; the
OpenAI adapter is a drop-in alternative.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class LLMError(Exception):
    """Raised when the provider call fails in a way the caller must handle."""


class LLMRefusal(LLMError):
    """The provider declined to answer for safety reasons."""


@dataclass(slots=True)
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    stop_reason: str | None = None
    cache_read_tokens: int = 0


class LLMClient(Protocol):
    """Minimal surface the generation chain needs from a provider."""

    async def complete(self, system: str, user_message: str) -> LLMResponse: ...

    async def count_tokens(self, system: str, user_message: str) -> int: ...


class AnthropicClient:
    """Adapter for the Anthropic Messages API."""

    def __init__(self) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None)
        self._model = settings.anthropic_model

    async def complete(self, system: str, user_message: str) -> LLMResponse:
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=settings.llm_max_tokens,
                # The system prompt is byte-stable across every request, so caching
                # it turns the instruction block into a ~0.1x-cost cache read.
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                output_config={"effort": settings.llm_effort},
                messages=[{"role": "user", "content": user_message}],
            )
        except self._anthropic.BadRequestError as exc:
            raise LLMError(f"Malformed request to Anthropic: {exc.message}") from exc
        except self._anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic API key is missing or invalid") from exc
        except self._anthropic.RateLimitError as exc:
            raise LLMError("Rate limited by Anthropic; retry shortly") from exc
        except self._anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise LLMError("Could not reach the Anthropic API") from exc

        if response.stop_reason == "refusal":
            detail = getattr(response.stop_details, "explanation", None) or "safety refusal"
            raise LLMRefusal(detail)

        text = "".join(block.text for block in response.content if block.type == "text")
        return LLMResponse(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
            stop_reason=response.stop_reason,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        )

    async def count_tokens(self, system: str, user_message: str) -> int:
        """Exact input token count, used to keep the context within budget."""
        result = await self._client.messages.count_tokens(
            model=self._model,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        return result.input_tokens


class OpenAIClient:
    """Adapter for the OpenAI Chat Completions API."""

    def __init__(self) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=settings.openai_api_key or None)
        self._model = settings.openai_model

    async def complete(self, system: str, user_message: str) -> LLMResponse:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=settings.llm_max_tokens,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
            )
        except Exception as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc

        choice = response.choices[0]
        return LLMResponse(
            text=choice.message.content or "",
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
            model=response.model,
            stop_reason=choice.finish_reason,
        )

    async def count_tokens(self, system: str, user_message: str) -> int:
        # OpenAI has no counting endpoint; approximate at ~4 characters per token.
        return (len(system) + len(user_message)) // 4


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    """Return the configured provider adapter, constructed once per process."""
    if settings.llm_provider == "openai":
        logger.info("Using OpenAI provider (%s)", settings.openai_model)
        return OpenAIClient()
    logger.info("Using Anthropic provider (%s)", settings.anthropic_model)
    return AnthropicClient()
