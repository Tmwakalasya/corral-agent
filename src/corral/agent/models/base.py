"""Provider-neutral types for one model turn.

History is kept in the Anthropic Messages shape (role + content blocks),
because that shape can carry everything any provider returns. Other
providers convert to and from it at their edge.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read + self.cache_write


@dataclass
class TurnResult:
    content: list[dict]
    stop_reason: str  # end_turn | tool_use | max_tokens | refusal | pause_turn
    usage: Usage
    model: str
    refusal: str | None = None
    notes: list[str] = field(default_factory=list)


class Sink(Protocol):
    """Receives streaming output while a turn is in flight."""

    def text(self, delta: str) -> None: ...
    def note(self, delta: str) -> None: ...
    def block_end(self) -> None: ...
    def tool_start(self, call_id: str, name: str) -> None: ...


class ProviderError(RuntimeError):
    """A failure worth showing the user verbatim (bad key, unknown model, outage)."""


class Provider(ABC):
    name: str

    @abstractmethod
    async def turn(
        self,
        *,
        model: str,
        system: str,
        tools: list[dict],
        messages: list[dict],
        sink: Sink,
        effort: str,
    ) -> TurnResult: ...

    async def aclose(self) -> None:
        return None
