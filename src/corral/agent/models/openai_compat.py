"""Any OpenAI-compatible chat completions endpoint: Ollama, OpenAI, OpenRouter,
Groq, Together, Fireworks, DeepSeek, xAI, Gemini's compat layer, vLLM.

Converts the Anthropic-shaped history at the edge. Thinking blocks from
Claude turns are dropped in conversion; other providers can't read them.
"""

from __future__ import annotations

import json
import uuid

import httpx2 as httpx

from .base import Provider, ProviderError, Sink, TurnResult, Usage


def to_openai(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [
                {"id": b["id"], "type": "function",
                 "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
                for b in content if b.get("type") == "tool_use"
            ]
            msg: dict = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            if text or calls:
                out.append(msg)
        elif m["role"] == "system":
            out.append({"role": "system", "content": "".join(b.get("text", "") for b in content)})
        else:
            texts = []
            for b in content:
                if b.get("type") == "tool_result":
                    body = b.get("content", "")
                    if isinstance(body, list):
                        body = "".join(x.get("text", "") for x in body if isinstance(x, dict))
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": str(body)})
                elif b.get("type") == "text":
                    texts.append(b["text"])
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
    return out


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [
        {"type": "function", "function": {"name": t["name"], "description": t["description"],
                                          "parameters": t["input_schema"]}}
        for t in tools
    ]


class _ThinkSplitter:
    """Routes ``<think>…</think>`` spans inside content to notes (older Ollama builds)."""

    def __init__(self, sink: Sink):
        self.sink = sink
        self.inside = False
        self.buf = ""
        self.text = ""

    def feed(self, s: str) -> None:
        self.buf += s
        while self.buf:
            tag = "</think>" if self.inside else "<think>"
            idx = self.buf.find(tag)
            if idx == -1:
                # Hold back a possible partial tag.
                keep = next((k for k in range(len(tag) - 1, 0, -1) if self.buf.endswith(tag[:k])), 0)
                emit, self.buf = (self.buf[:-keep], self.buf[-keep:]) if keep else (self.buf, "")
                self._emit(emit)
                return
            self._emit(self.buf[:idx])
            self.buf = self.buf[idx + len(tag):]
            if self.inside:
                self.sink.block_end()
            self.inside = not self.inside

    def _emit(self, s: str) -> None:
        if not s:
            return
        if self.inside:
            self.sink.note(s)
        else:
            self.text += s
            self.sink.text(s)

    def flush(self) -> None:
        self._emit(self.buf)
        self.buf = ""


class OpenAICompatProvider(Provider):
    def __init__(self, name: str, base_url: str, api_key: str | None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))

    async def turn(self, *, model: str, system: str, tools: list[dict], messages: list[dict], sink: Sink,
                   effort: str) -> TurnResult:
        body = {
            "model": model,
            "messages": to_openai(system, messages),
            "tools": to_openai_tools(tools),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        splitter = _ThinkSplitter(sink)
        calls: dict[int, dict] = {}
        finish = None
        usage = Usage()
        served = model
        in_reasoning = False
        try:
            async with self.client.stream("POST", f"{self.base_url}/chat/completions", json=body, headers=headers) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode(errors="replace")[:400]
                    raise ProviderError(f"{self.name} returned HTTP {r.status_code}: {detail}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    served = chunk.get("model") or served
                    if chunk.get("usage"):
                        u = chunk["usage"]
                        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                        usage = Usage(input_tokens=(u.get("prompt_tokens") or 0) - cached,
                                      output_tokens=u.get("completion_tokens") or 0, cache_read=cached)
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            in_reasoning = True
                            sink.note(reasoning)
                        if delta.get("content"):
                            if in_reasoning:
                                sink.block_end()
                                in_reasoning = False
                            splitter.feed(delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", len(calls)), {"id": None, "name": "", "args": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name") and not slot["name"]:
                                slot["name"] = fn["name"]
                                sink.tool_start(slot["id"] or "", slot["name"])
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"] if isinstance(fn["arguments"], str) else json.dumps(fn["arguments"])
                        finish = choice.get("finish_reason") or finish
        except httpx.ConnectError as e:
            raise ProviderError(f"Couldn't connect to {self.name} at {self.base_url}.") from e
        except httpx.TimeoutException as e:
            raise ProviderError(f"{self.name} timed out.") from e
        splitter.flush()
        if in_reasoning:
            sink.block_end()
        if splitter.text:
            sink.block_end()

        content: list[dict] = []
        if splitter.text.strip():
            content.append({"type": "text", "text": splitter.text})
        for _, slot in sorted(calls.items()):
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except json.JSONDecodeError:
                args = {"__invalid_json__": slot["args"][:2000]}
            content.append({"type": "tool_use", "id": slot["id"] or f"call_{uuid.uuid4().hex[:12]}",
                            "name": slot["name"], "input": args})
        has_tools = any(b["type"] == "tool_use" for b in content)
        stop = {"length": "max_tokens"}.get(finish or "", "tool_use" if has_tools else "end_turn")
        return TurnResult(content=content, stop_reason=stop, usage=usage, model=served)

    async def aclose(self) -> None:
        await self.client.aclose()
