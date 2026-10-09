"""The agent loop: model turn → tools → model turn, until the model stops.

Tool calls run one at a time, in the order the model issued them (GitFarm's
ordered-exec guarantee: one session, one consistent repo state, commands in
submission order). History is append-only; every tool_use gets exactly one
tool_result, including when a turn is cancelled.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from ..audit import AuditLog
from ..config import Config
from .models import ProviderError, Router, Usage
from .models import pricing
from .tools import TOOLS, Toolbox, describe

Emit = Callable[..., None]


class _Sink:
    def __init__(self, emit: Emit):
        self.emit = emit

    def text(self, delta: str) -> None:
        self.emit("text.delta", text=delta)

    def note(self, delta: str) -> None:
        self.emit("note.delta", text=delta)

    def block_end(self) -> None:
        self.emit("block.end")

    def tool_start(self, call_id: str, name: str) -> None:
        self.emit("tool.preparing", call_id=call_id, name=name)


class Agent:
    def __init__(self, *, cfg: Config, router: Router, model: str, system: str, toolbox: Toolbox, emit: Emit,
                 audit: AuditLog, budget_usd: float):
        self.cfg = cfg
        self.router = router
        self.model = model
        self.effort = cfg.effort
        self.system = system
        self.toolbox = toolbox
        self.emit = emit
        self.audit = audit
        self.budget_usd = budget_usd
        self.messages: list[dict] = []
        self.usage = Usage()
        self.cost = 0.0
        self.unpriced_tokens = 0
        self.last_prompt_tokens = 0

    def stats(self) -> dict:
        window = pricing.context_window(self.model)
        return {
            "model": self.model,
            "input_tokens": self.usage.prompt_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read": self.usage.cache_read,
            "cost": round(self.cost, 4),
            "budget": self.budget_usd,
            "priced": self.unpriced_tokens == 0,
            "context_pct": round(100 * self.last_prompt_tokens / window, 1) if window else 0,
        }

    async def run_turn(self, text: str) -> None:
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
        self.audit.write("user", text=text)
        self.emit("turn.start")
        stop = "end_turn"
        pending: list[dict] = []
        results: list[dict] = []
        try:
            for _ in range(self.cfg.max_steps):
                if self.cost >= self.budget_usd:
                    self.emit("notice", level="warn",
                              text=f"Budget reached: ${self.cost:.2f} of ${self.budget_usd:.2f}. Raise it with /budget.")
                    stop = "budget"
                    break
                provider, api_model = self.router.resolve(self.model)
                result = await provider.turn(model=api_model, system=self.system, tools=TOOLS,
                                             messages=self.messages, sink=_Sink(self.emit), effort=self.effort)
                if result.content:
                    self.messages.append({"role": "assistant", "content": result.content})
                self._account(result.usage)
                self.audit.write("model", model=self.model, served_by=result.model, stop=result.stop_reason,
                                 usage=vars(result.usage))
                if result.stop_reason == "refusal":
                    self.emit("notice", level="error", text=f"The model declined this request ({result.refusal}).")
                    stop = "refusal"
                    break
                pending = [b for b in result.content if b.get("type") == "tool_use"]
                if not pending:
                    stop = result.stop_reason
                    break
                results = []
                if result.stop_reason == "max_tokens":
                    for block in pending:
                        results.append(self._result(block["id"], "The response hit the output limit before this "
                                                    "tool input was complete. Retry with smaller pieces.", True))
                    pending = []
                    self.messages.append({"role": "user", "content": results})
                    continue
                for block in list(pending):
                    results.append(await self._call(block))
                    pending.remove(block)
                self.messages.append({"role": "user", "content": results})
                results = []
            else:
                self.emit("notice", level="warn", text=f"Stopped after {self.cfg.max_steps} steps.")
                stop = "max_steps"
        except asyncio.CancelledError:
            if pending:
                await self.toolbox.cancel_running()
                for block in pending:
                    results.append(self._result(block["id"], "Cancelled by the user.", True))
                    self.emit("tool.end", call_id=block["id"], ok=False, summary="cancelled", detail="")
                self.messages.append({"role": "user", "content": results})
            self.audit.write("cancel")
            self.emit("turn.end", stop="cancelled")
            raise
        except ProviderError as e:
            self.emit("notice", level="error", text=str(e))
            stop = "error"
        self.emit("turn.end", stop=stop)

    def _result(self, call_id: str, content: str, is_error: bool) -> dict:
        r = {"type": "tool_result", "tool_use_id": call_id, "content": content}
        if is_error:
            r["is_error"] = True
        return r

    async def _call(self, block: dict) -> dict:
        name, args, call_id = block.get("name", ""), block.get("input", {}), block["id"]
        self.emit("tool.start", call_id=call_id, name=name, title=describe(name, args))
        started = time.monotonic()
        try:
            outcome = await self.toolbox.run(call_id, name, args)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a tool failing must not end the session
            self.emit("tool.end", call_id=call_id, ok=False, summary="failed", detail=str(e)[:500])
            self.audit.write("tool", name=name, input=args, ok=False, error=str(e)[:500])
            return self._result(call_id, f"Tool failed: {e}", True)
        self.emit("tool.end", call_id=call_id, ok=not outcome.is_error, summary=outcome.summary,
                  detail=outcome.detail, seconds=round(time.monotonic() - started, 2))
        self.audit.write("tool", name=name, input=args, ok=not outcome.is_error, summary=outcome.summary)
        return self._result(call_id, outcome.content, outcome.is_error)

    def _account(self, usage: Usage) -> None:
        self.usage.add(usage)
        self.last_prompt_tokens = usage.prompt_tokens + usage.output_tokens
        c = pricing.cost(self.model, usage, self.cfg.pricing)
        if c is None:
            self.unpriced_tokens += usage.prompt_tokens + usage.output_tokens
        else:
            self.cost += c
        self.emit("usage", **self.stats())
