"""Model routing by name.

``claude-*``            → Anthropic (official SDK)
``ollama/<model>``      → local Ollama, no key
``openai/<model>``, ``gpt-*``, ``o<digit>*`` → OpenAI
``openrouter/…``, ``groq/…``, ``together/…``, ``fireworks/…``,
``deepseek/…``, ``xai/…``, ``gemini/…`` → their OpenAI-compatible APIs
``<name>/<model>`` where ``[providers.<name>]`` is in config.toml → that endpoint
"""

from __future__ import annotations

import os
import re

from ... import keys
from ...config import Config
from .base import Provider, ProviderError, Sink, TurnResult, Usage

BUILTIN = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "xai": "https://api.x.ai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}

__all__ = ["Router", "Provider", "ProviderError", "Sink", "TurnResult", "Usage"]


class Router:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cache: dict[str, Provider] = {}

    def split(self, model: str) -> tuple[str, str]:
        """(provider name, model id to send)."""
        if model.startswith("claude-"):
            return "anthropic", model
        if "/" in model:
            prefix, rest = model.split("/", 1)
            if prefix in ("ollama", "anthropic") or prefix in BUILTIN or prefix in self.cfg.providers:
                return prefix, rest
        if model.startswith("gpt-") or re.match(r"o\d", model):
            return "openai", model
        if model.startswith("grok-"):
            return "xai", model
        raise ProviderError(f"Don't know which provider serves {model!r}. Use a prefix like ollama/ or openrouter/.")

    def resolve(self, model: str) -> tuple[Provider, str]:
        name, api_model = self.split(model)
        if name not in self._cache:
            self._cache[name] = self._build(name)
        return self._cache[name], api_model

    def _build(self, name: str) -> Provider:
        from .anthropic_provider import AnthropicProvider
        from .openai_compat import OpenAICompatProvider

        if name == "anthropic":
            return AnthropicProvider(keys.get("anthropic"))
        if name == "ollama":
            base = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
            if not base.startswith("http"):
                base = "http://" + base
            return OpenAICompatProvider("ollama", base + "/v1", None)
        if name in self.cfg.providers:
            p = self.cfg.providers[name]
            key = os.environ.get(p.key_env) if p.key_env else None
            return OpenAICompatProvider(name, p.base_url, key or keys.get(name, p.key_env))
        key = keys.get(name)
        if not key:
            env = keys.ENV_VARS.get(name, name.upper() + "_API_KEY")
            raise ProviderError(f"No key for {name}. Run `corral key set {name}` or set {env}.")
        return OpenAICompatProvider(name, BUILTIN[name], key)

    async def aclose(self) -> None:
        for p in self._cache.values():
            await p.aclose()
        self._cache.clear()
