"""Claude via the official Anthropic SDK.

- Adaptive thinking. On models that support it, ``display: "updates"`` turns
  the notes the model writes between tool calls into short visible text.
- Server-side refusal fallbacks (``fallbacks: "default"``) on the models
  that take them.
- History is append-only and assistant content is replayed exactly as
  returned, thinking blocks included, so cached prefixes and preserved
  thinking stay valid.
"""

from __future__ import annotations

import anthropic

from .base import Provider, ProviderError, Sink, TurnResult, Usage

UPDATES_DISPLAY = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5", "claude-mythos-5-1"}
SERVER_FALLBACKS = {"claude-opus-5-5", "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5-5"}
ADAPTIVE_PREFIXES = (
    "claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-5-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
)
MAX_TOKENS = 64_000


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, api_key: str | None):
        try:
            self.client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        except Exception as e:  # no credentials anywhere
            raise ProviderError(
                "No Anthropic credentials. Run `corral key set anthropic` or set ANTHROPIC_API_KEY."
            ) from e

    def _params(self, model: str, system: str, tools: list[dict], messages: list[dict], effort: str) -> tuple[dict, list[str]]:
        params: dict = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": [{"type": "text", "text": system}],
            "tools": [{**t, "eager_input_streaming": True} for t in tools],
            "messages": messages,
            "cache_control": {"type": "ephemeral"},
        }
        betas: list[str] = []
        if model.startswith(ADAPTIVE_PREFIXES):
            if model in UPDATES_DISPLAY:
                params["thinking"] = {"type": "adaptive", "display": "updates"}
                betas.append("thinking-display-updates-2026-08-18")
            else:
                params["thinking"] = {"type": "adaptive", "display": "summarized"}
            params["output_config"] = {"effort": effort}
        if model in SERVER_FALLBACKS:
            params["fallbacks"] = "default"
            betas.append("server-side-fallback-2026-07-01")
        return params, betas

    async def turn(self, *, model: str, system: str, tools: list[dict], messages: list[dict], sink: Sink,
                   effort: str) -> TurnResult:
        params, betas = self._params(model, system, tools, messages, effort)
        json_retries = 0
        while True:
            try:
                async with self.client.beta.messages.stream(**params, betas=betas) as stream:
                    open_kind = None
                    async for event in stream:
                        if event.type == "content_block_start":
                            block = event.content_block
                            open_kind = block.type
                            if block.type == "tool_use":
                                sink.tool_start(block.id, block.name)
                        elif event.type == "content_block_delta":
                            delta = event.delta
                            if delta.type == "text_delta":
                                sink.text(delta.text)
                            elif delta.type == "thinking_delta" and delta.thinking:
                                sink.note(delta.thinking)
                        elif event.type == "content_block_stop":
                            if open_kind in ("text", "thinking"):
                                sink.block_end()
                            open_kind = None
                    final = await stream.get_final_message()
                break
            except ValueError:
                # Tool input JSON the SDK couldn't parse at all. There's no
                # complete tool_use block to answer, so re-issue the turn.
                json_retries += 1
                if json_retries > 2:
                    raise ProviderError("The model produced unreadable tool input three times in a row.")
                continue
            except anthropic.AuthenticationError as e:
                raise ProviderError("Anthropic rejected the API key. Run `corral key set anthropic`.") from e
            except anthropic.PermissionDeniedError as e:
                raise ProviderError(f"Anthropic denied access to {model}: {e.message}") from e
            except anthropic.NotFoundError as e:
                raise ProviderError(f"Unknown model or endpoint: {model}") from e
            except anthropic.BadRequestError as e:
                raise ProviderError(f"Anthropic rejected the request: {e.message}") from e
            except anthropic.RateLimitError as e:
                raise ProviderError("Rate limited by Anthropic after retries. Wait a moment and send again.") from e
            except anthropic.APIStatusError as e:
                raise ProviderError(f"Anthropic API error {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise ProviderError("Couldn't reach the Anthropic API. Check your connection.") from e

        u = final.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        refusal = None
        if final.stop_reason == "refusal":
            details = final.stop_details
            category = getattr(details, "category", None) if details else None
            explanation = getattr(details, "explanation", None) if details else None
            refusal = ", ".join(x for x in (category, explanation) if x) or "no details"
        content = [block.to_dict() for block in final.content]
        return TurnResult(content=content, stop_reason=final.stop_reason or "end_turn", usage=usage,
                          model=final.model, refusal=refusal)

    async def aclose(self) -> None:
        await self.client.close()
