"""LiteLLM provider implementation for multi-provider support."""

import json
import os
from typing import Any

import litellm
from litellm import acompletion

from .base import LLMProvider, LLMResponse, ToolCallRequest
from .registry import provider_by_name


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.

    Provider-specific logic is driven by the registry (see providers/registry.py).
    """

    def __init__(
        self,
        provider_name: str,
        api_key: str,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self._spec = provider_by_name(provider_name)

        if api_key:
            self._setup_env(api_key, api_base)

        # For gateways/local, set api_base from config or spec default.
        # Standard providers set their base via env vars in _setup_env instead,
        # to avoid polluting the global litellm.api_base.
        effective_base = api_base
        if not effective_base and self._spec and (self._spec.is_gateway or self._spec.is_local):
            effective_base = self._spec.default_api_base or None
        if effective_base:
            litellm.api_base = effective_base

        litellm.suppress_debug_info = True
        litellm.drop_params = True

    def _setup_env(self, api_key: str, api_base: str | None) -> None:
        """Set environment variables for the configured provider."""
        if not self._spec:
            return
        os.environ[self._spec.env_key] = api_key

        effective_base = api_base or self._spec.default_api_base
        for env_name, env_val in self._spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key).replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)

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
        model = model or self.default_model
        max_tokens = max(1, max_tokens)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "api_key": self.api_key,
            "api_base": self.api_base,
            "extra_headers": self.extra_headers,
            "tools": tools,
            "tool_choice": "auto",
        }

        self._apply_model_overrides(model, kwargs)

        try:
            response = await acompletion(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            return LLMResponse(
                content=f"Error calling LLM: {str(e)}",
                finish_reason="error",
            )

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        choice = response.choices[0]
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
                        name=tc.function.name,
                        arguments=args,
                    )
                )

        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            reasoning_content=getattr(message, "reasoning_content", None),
        )
