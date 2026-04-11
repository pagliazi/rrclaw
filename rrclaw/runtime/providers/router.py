"""
Provider Router — prefix routing + fallback chain.

Routes model strings like "qwen3.5-plus" to the right provider.
Falls back on consecutive 529 errors.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

from rrclaw.runtime.providers.base import BaseLLMProvider
from rrclaw.runtime.resilience.api_retry import ApiRetryPolicy

logger = logging.getLogger("rrclaw.providers.router")


class ProviderRouter(BaseLLMProvider):
    """
    Multi-provider router with fallback chain.

    Config:
        providers:
            primary: qwen3.5-plus
            fallback_chain:
                - dashscope/qwen3.5-plus
                - ollama/qwen2.5-coder:14b
    """

    def __init__(self, config: Any):
        self.config = config
        self._providers: dict[str, BaseLLMProvider] = {}
        self._retry = ApiRetryPolicy()

        primary = config.get("providers", "primary", default="qwen3.5-plus")
        fallback_chain = config.get("providers", "fallback_chain", default=[])

        self._model_chain = [primary] + (fallback_chain if isinstance(fallback_chain, list) else [])
        self._current_index = 0

        # Pre-initialize primary provider
        self._get_provider(primary)

    def _get_provider(self, model_str: str) -> BaseLLMProvider:
        """Get or create provider for a model string."""
        if model_str in self._providers:
            return self._providers[model_str]

        prefix = model_str.split("/")[0] if "/" in model_str else "anthropic"

        if prefix == "anthropic":
            from rrclaw.runtime.providers.anthropic import AnthropicProvider
            provider = AnthropicProvider(model=model_str)
        elif prefix == "dashscope":
            from rrclaw.runtime.providers.dashscope import DashScopeProvider
            provider = DashScopeProvider(model=model_str)
        elif prefix == "ollama":
            from rrclaw.runtime.providers.openai_compat import OpenAICompatProvider
            provider = OpenAICompatProvider(model=model_str)
        else:
            # Default to OpenAI-compatible
            from rrclaw.runtime.providers.openai_compat import OpenAICompatProvider
            provider = OpenAICompatProvider(model=model_str)

        self._providers[model_str] = provider
        return provider

    @property
    def current_model(self) -> str:
        return self._model_chain[self._current_index]

    async def stream(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        model: str,
    ) -> AsyncGenerator[dict, None]:
        """Stream with retry and provider fallback."""
        target_model = model or self.current_model
        provider = self._get_provider(target_model)

        async def _do_stream():
            results = []
            async for chunk in provider.stream(messages, system, tools, target_model):
                results.append(chunk)
            return results

        try:
            chunks = await self._retry.call_with_retry(
                _do_stream,
                on_model_fallback=self._fallback,
                on_credential_rotate=lambda: provider.rotate_credential(),
            )
            for chunk in chunks:
                yield chunk
        except Exception:
            # Try fallback directly
            if self.switch_to_fallback():
                fallback_model = self.current_model
                fallback_provider = self._get_provider(fallback_model)
                async for chunk in fallback_provider.stream(
                    messages, system, tools, fallback_model
                ):
                    yield chunk
            else:
                raise

    async def complete(self, messages, system, tools, model) -> dict:
        target_model = model or self.current_model
        provider = self._get_provider(target_model)
        return await provider.complete(messages, system, tools, target_model)

    def rotate_credential(self):
        provider = self._get_provider(self.current_model)
        provider.rotate_credential()

    def switch_to_fallback(self) -> bool:
        if self._current_index < len(self._model_chain) - 1:
            self._current_index += 1
            logger.warning(
                f"Switched to fallback provider: {self.current_model} "
                f"(index {self._current_index}/{len(self._model_chain) - 1})"
            )
            return True
        return False

    def _fallback(self):
        """Called by retry policy on consecutive 529s."""
        self.switch_to_fallback()

    def reset_to_primary(self):
        """Reset to primary provider."""
        self._current_index = 0
