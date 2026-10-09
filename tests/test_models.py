import json

import httpx2

from corral.agent.models import Router
from corral.agent.models.anthropic_provider import AnthropicProvider
from corral.agent.models.openai_compat import OpenAICompatProvider, to_openai
from corral.agent.models import pricing
from corral.agent.models.base import Usage
from corral.agent.tools import TOOLS
from corral.config import Config


class Sink:
    def __init__(self):
        self.text_, self.notes, self.tools, self.ends = "", "", [], 0

    def text(self, d):
        self.text_ += d

    def note(self, d):
        self.notes += d

    def block_end(self):
        self.ends += 1

    def tool_start(self, call_id, name):
        self.tools.append((call_id, name))


def test_routing():
    r = Router(Config())
    assert r.split("claude-opus-5-5") == ("anthropic", "claude-opus-5-5")
    assert r.split("ollama/qwen3:8b") == ("ollama", "qwen3:8b")
    assert r.split("openrouter/moonshotai/kimi-k3") == ("openrouter", "moonshotai/kimi-k3")
    assert r.split("gpt-5") == ("openai", "gpt-5")
    try:
        r.split("mystery-model")
        assert False
    except Exception as e:
        assert "prefix" in str(e)


def test_history_conversion_drops_thinking_and_pairs_tools():
    history = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls"}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py"}]},
    ]
    out = to_openai("sys", history)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    assert out[2]["tool_calls"][0]["function"] == {"name": "bash", "arguments": '{"command": "ls"}'}
    assert "thinking" not in json.dumps(out)
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "a.py"}


def _sse(chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


async def test_openai_stream_parsing():
    chunks = [
        {"choices": [{"delta": {"reasoning": "thinking about it"}}]},
        {"choices": [{"delta": {"content": "Running "}}]},
        {"choices": [{"delta": {"content": "tests."}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "bash", "arguments": "{\"comm"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "and\": \"ls\"}"}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 30}},
    ]
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx2.Response(200, text=_sse(chunks), headers={"content-type": "text/event-stream"})

    p = OpenAICompatProvider("test", "https://example.invalid/v1", "k")
    p.client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    sink = Sink()
    r = await p.turn(model="m", system="s", tools=TOOLS, messages=[{"role": "user", "content": "x"}], sink=sink,
                     effort="high")
    assert seen["auth"] == "Bearer k" and seen["body"]["stream"] is True
    assert sink.notes == "thinking about it" and sink.text_ == "Running tests."
    assert r.stop_reason == "tool_use"
    assert r.content == [{"type": "text", "text": "Running tests."},
                         {"type": "tool_use", "id": "c1", "name": "bash", "input": {"command": "ls"}}]
    assert (r.usage.input_tokens, r.usage.output_tokens) == (120, 30)


async def test_think_tags_in_content_become_notes():
    chunks = [{"choices": [{"delta": {"content": "<thi"}}]}, {"choices": [{"delta": {"content": "nk>plan</think>Done"}}]}]
    p = OpenAICompatProvider("t", "https://x.invalid/v1", None)
    p.client = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: httpx2.Response(200, text=_sse(chunks))))
    sink = Sink()
    r = await p.turn(model="m", system="s", tools=[], messages=[], sink=sink, effort="high")
    assert sink.notes == "plan" and r.content == [{"type": "text", "text": "Done"}] and r.stop_reason == "end_turn"


def test_claude_request_shape():
    p = AnthropicProvider("sk-test")
    params, betas = p._params("claude-opus-5-5", "sys", TOOLS[:1], [], "high")
    assert params["thinking"] == {"type": "adaptive", "display": "updates"}
    assert params["output_config"] == {"effort": "high"}
    assert params["fallbacks"] == "default"
    assert params["cache_control"] == {"type": "ephemeral"}
    assert params["tools"][0]["eager_input_streaming"] is True
    assert set(betas) == {"thinking-display-updates-2026-08-18", "server-side-fallback-2026-07-01"}
    params, betas = p._params("claude-haiku-4-5", "sys", [], [], "high")
    assert "thinking" not in params and "fallbacks" not in params and betas == []


def test_pricing():
    assert pricing.cost("claude-opus-5-5", Usage(1_000_000, 100_000, 0, 0)) == 4.0 + 2.0
    assert pricing.cost("ollama/qwen3:8b", Usage(10**6, 10**6)) == 0.0
    assert pricing.cost("openrouter/x", Usage(1, 1)) is None


def _anthropic_sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


async def test_claude_stream_through_sdk():
    import anthropic

    events = [
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 500,
                      "cache_creation_input_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Checking the test."}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig123"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Running it."}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2,
         "content_block": {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"command\": "}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "\"ls\"}"}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 42}},
        {"type": "message_stop"},
    ]
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        seen["path"] = request.url.path
        return httpx2.Response(200, text=_anthropic_sse(events), headers={"content-type": "text/event-stream"})

    p = AnthropicProvider("sk-test")
    p.client = anthropic.AsyncAnthropic(
        api_key="sk-test", max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))
    sink = Sink()
    history = [{"role": "user", "content": [{"type": "text", "text": "run the tests"}]}]
    r = await p.turn(model="claude-opus-5-5", system="sys", tools=TOOLS[:1], messages=history, sink=sink,
                     effort="high")

    body = seen["body"]
    assert seen["path"] == "/v1/messages"
    assert {"thinking-display-updates-2026-08-18", "server-side-fallback-2026-07-01"} <= set(seen["beta"].split(","))
    assert body["thinking"] == {"type": "adaptive", "display": "updates"}
    assert body["fallbacks"] == "default" and body["cache_control"] == {"type": "ephemeral"}
    assert body["output_config"] == {"effort": "high"} and body["stream"] is True
    assert body["tools"][0]["eager_input_streaming"] is True

    assert sink.notes == "Checking the test." and sink.text_ == "Running it."
    assert sink.tools == [("toolu_1", "bash")]
    assert r.stop_reason == "tool_use"
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cache_read) == (10, 42, 500)
    thinking, text, tool = r.content
    # Replayed exactly: the thinking block keeps its signature.
    assert thinking["type"] == "thinking" and thinking["signature"] == "sig123"
    assert text["type"] == "text" and text["text"] == "Running it."
    assert tool == {**tool, "type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "ls"}}
    # The dicts go straight back into the next request.
    json.dumps(r.content)
