"""Prices (USD per million tokens) and context windows.

Claude prices are Anthropic first-party API rates. Anything not listed is
priced from ``[pricing]`` in config.toml, or treated as unknown (local
models are free).
"""

from __future__ import annotations

from .base import Usage

# model: (input, output, cache_read)
CLAUDE = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
CACHE_WRITE_MULTIPLIER = 1.25


def price(model: str, overrides: dict | None = None) -> tuple[float, float, float] | None:
    if overrides and model in overrides:
        p = overrides[model]
        return (p.input, p.output, p.input * 0.1)
    if model in CLAUDE:
        return CLAUDE[model]
    if model.startswith("ollama/"):
        return (0.0, 0.0, 0.0)
    return None


def cost(model: str, usage: Usage, overrides: dict | None = None) -> float | None:
    p = price(model, overrides)
    if p is None:
        return None
    inp, out, cache_read = p
    return (
        usage.input_tokens * inp
        + usage.output_tokens * out
        + usage.cache_read * cache_read
        + usage.cache_write * inp * CACHE_WRITE_MULTIPLIER
    ) / 1_000_000


def context_window(model: str) -> int:
    if model.startswith("claude-"):
        return 200_000 if model == "claude-haiku-4-5" else 1_000_000
    if model.startswith("ollama/"):
        return 32_768
    return 128_000
