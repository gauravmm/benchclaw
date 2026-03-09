"""LiteLLM provider implementation for multi-provider support."""

import json
import os
from typing import Any

import litellm
from litellm import acompletion
from loguru import logger

from benchclaw.config import ProviderConfig

from .base import LLMProvider, LLMResponse, ToolCallRequest
from .registry import provider_by_name


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.

    Provider-specific logic is driven by the registry (see providers/registry.py).
    """

    def __init__(
        self,
        p: ProviderConfig,
        default_model: str = "anthropic/claude-opus-4-5",
    ):
        super().__init__()
        self.default_model = default_model
        self._config = p
        self._spec = provider_by_name(p.name)

        if not p.api_key:
            logger.error("No API key configured.")
            logger.error("Set one in config/config.yaml under provider section.")
            raise RuntimeError("No API key configured")

        # Compute the effective base, use it to update the environment:
        self._effective_base = self._config.api_base or self._spec.default_api_base
        if self._spec.env_key:
            os.environ[self._spec.env_key] = self._config.api_key
        for env_name, env_val in self._spec.env_extras:
            resolved = env_val.replace("{api_key}", self._config.api_key).replace(
                "{api_base}", self._effective_base
            )
            os.environ.setdefault(env_name, resolved)

        # Set up litellm options.
        litellm.api_base = self._effective_base
        litellm.suppress_debug_info = True
        litellm.drop_params = True

        logger.info(f"Configured LiteLLMProvider with {self._config.name}.")

    def _apply_model_overrides(self, model: str, kwargs: dict[str, Any]) -> None:
        """Apply model-specific parameter overrides from the registry."""
        if not self._spec:
            return
        model_lower = model.lower()
        for pattern, overrides in self._spec.model_overrides:
            if pattern in model_lower:
                kwargs.update(overrides)
                return

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        assert max_tokens >= 1
        assert temperature >= 0
        model = model or self.default_model

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "api_key": self._config.api_key,
            "api_base": self._effective_base or None,
            "extra_headers": self._config.extra_headers,
            "tools": tools,
            "custom_llm_provider": self._spec.litellm_provider,
        }

        self._apply_model_overrides(model, kwargs)

        try:
            response = await acompletion(**kwargs)
            assert isinstance(response, litellm.ModelResponse)
            return self._parse_response(response)
        except Exception as e:
            return LLMResponse(
                content=f"Error calling LLM: {str(e)}",
                finish_reason="error",
            )

    def _parse_response(self, response: litellm.ModelResponse) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        choice = response.choices[0]
        assert isinstance(choice, litellm.Choices)
        message = choice.message

        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}

                tool_calls.append(
                    ToolCallRequest(
                        id=tc.id,
                        name=tc.function.name or "(no name)",
                        arguments=args,
                    )
                )

        usage = {}
        response_usage = getattr(response, "usage", None)
        if response_usage:
            usage = {
                "prompt_tokens": response_usage.prompt_tokens,
                "completion_tokens": response_usage.completion_tokens,
                "total_tokens": response_usage.total_tokens,
            }

        content = message.content if message.content is not None else ""

        if content.startswith("\n\n"):
            content = content.lstrip("\n")  # Fix for Qwen issue

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            reasoning_content=getattr(message, "reasoning_content", None),
        )
