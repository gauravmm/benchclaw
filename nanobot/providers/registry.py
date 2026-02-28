"""
Provider Registry — single source of truth for LLM provider metadata.

Adding a new provider:
  1. Add a ProviderSpec to PROVIDERS below.
  Done. Env vars, config matching, status display all derive from here.

Order matters — it controls display priority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderSpec:
    """One LLM provider's metadata. See PROVIDERS below for real examples.

    Placeholders in env_extras values:
      {api_key}  — the user's API key
      {api_base} — api_base from config
    """

    name: str  # config field name, e.g. "dashscope"
    env_key: str  # LiteLLM env var, e.g. "DASHSCOPE_API_KEY"
    display_name: str = ""

    # extra env vars, e.g. (("ZHIPUAI_API_KEY", "{api_key}"),)
    env_extras: tuple[tuple[str, str], ...] = ()

    is_gateway: bool = False  # routes any model (OpenRouter, AiHubMix)
    is_local: bool = False  # local deployment (vLLM, Ollama)
    default_api_base: str = ""

    # per-model param overrides, e.g. (("kimi-k2.5", {"temperature": 1.0}),)
    model_overrides: tuple[tuple[str, dict[str, Any]], ...] = ()

    @property
    def label(self) -> str:
        return self.display_name or self.name.title()


# ---------------------------------------------------------------------------
# PROVIDERS — the registry. Order = display priority. Copy any entry as template.
# ---------------------------------------------------------------------------

PROVIDERS: tuple[ProviderSpec, ...] = (
    # === Custom (user-provided OpenAI-compatible endpoint) =================
    ProviderSpec(
        name="custom",
        env_key="OPENAI_API_KEY",
        display_name="Custom",
        is_gateway=True,
    ),
    # === Gateways ==========================================================
    ProviderSpec(
        name="openrouter",
        env_key="OPENROUTER_API_KEY",
        display_name="OpenRouter",
        is_gateway=True,
        default_api_base="https://openrouter.ai/api/v1",
    ),
    ProviderSpec(
        name="aihubmix",
        env_key="OPENAI_API_KEY",
        display_name="AiHubMix",
        is_gateway=True,
        default_api_base="https://aihubmix.com/v1",
    ),
    # === Standard providers ================================================
    ProviderSpec(
        name="anthropic",
        env_key="ANTHROPIC_API_KEY",
        display_name="Anthropic",
    ),
    ProviderSpec(
        name="openai",
        env_key="OPENAI_API_KEY",
        display_name="OpenAI",
    ),
    ProviderSpec(
        name="deepseek",
        env_key="DEEPSEEK_API_KEY",
        display_name="DeepSeek",
    ),
    ProviderSpec(
        name="gemini",
        env_key="GEMINI_API_KEY",
        display_name="Gemini",
    ),
    ProviderSpec(
        name="zhipu",
        env_key="ZAI_API_KEY",
        display_name="Zhipu AI",
        env_extras=(("ZHIPUAI_API_KEY", "{api_key}"),),
    ),
    ProviderSpec(
        name="dashscope",
        env_key="DASHSCOPE_API_KEY",
        display_name="DashScope",
    ),
    ProviderSpec(
        name="moonshot",
        env_key="MOONSHOT_API_KEY",
        display_name="Moonshot",
        env_extras=(("MOONSHOT_API_BASE", "{api_base}"),),
        default_api_base="https://api.moonshot.ai/v1",
        model_overrides=(("kimi-k2.5", {"temperature": 1.0}),),
    ),
    ProviderSpec(
        name="minimax",
        env_key="MINIMAX_API_KEY",
        display_name="MiniMax",
        default_api_base="https://api.minimax.io/v1",
    ),
    # === Local deployment ==================================================
    ProviderSpec(
        name="vllm",
        env_key="HOSTED_VLLM_API_KEY",
        display_name="vLLM/Local",
        is_local=True,
    ),
    # === Auxiliary =========================================================
    ProviderSpec(
        name="groq",
        env_key="GROQ_API_KEY",
        display_name="Groq",
    ),
)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def provider_by_name(name: str) -> ProviderSpec:
    """Find a provider spec by name, e.g. "dashscope"."""
    for spec in PROVIDERS:
        if spec.name == name:
            return spec

    raise RuntimeError(
        f"LLM Provider {name} not found. Valid names are: {', '.join(p.name for p in PROVIDERS)}"
    )
