"""The agent's tools. Every one of them executes inside the cell.

``request_network`` is the only tool that reaches outside: it asks the
gateway, which asks the user. Its result is whatever the user decided.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..backend.cells import Cell
from ..text import one_line, sanitize, truncate_middle

MAX_RESULT = 30_000
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
HOST_RE = re.compile(r"(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")

TOOLS: list[dict] = [
    {
        "name": "bash",
        "description": (
            "Run a bash command in the cell. The working directory is /workspace, a copy of the user's repository. "
            "Network access is blocked except for hosts the user has approved (see request_network). "
            f"Commands time out after timeout_s seconds (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT}). "
            f"Output longer than {MAX_RESULT} characters is cut in the middle. Don't start interactive programs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run."},
                "timeout_s": {"type": "integer", "description": "Timeout in seconds."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file. Returns numbered lines. Use offset (1-based line) and limit for large files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": "Create a file, or overwrite one completely. Prefer edit_file for changes to existing files.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace an exact string in a file. old_string must occur exactly once unless replace_all is true; "
            "include enough surrounding lines to make it unique. Read the file first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search",
        "description": "Search file contents with a ripgrep regex. Returns path:line:text. Respects .gitignore.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Directory or file to search. Default: the repository."},
                "glob": {"type": "string", "description": "Only files matching this glob, e.g. '*.py'."},
                "ignore_case": {"type": "boolean"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_files",
        "description": "List files under a directory, respecting .gitignore. Optional glob pattern, e.g. 'src/**/*.ts'.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "pattern": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "request_network",
        "description": (
            "Ask the user to allow HTTPS access to specific hosts, for example pypi.org and files.pythonhosted.org "
            "for pip, or registry.npmjs.org for npm. Name exact hosts; '*.example.com' covers subdomains. "
            "Call this when a command fails because the network is blocked. The user sees the hosts and your "
            "reason and may deny. Returns the hosts that are now allowed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hosts": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string", "description": "One sentence: what you need these hosts for."},
            },
            "required": ["hosts", "reason"],
            "additionalProperties": False,
        },
    },
]

_TYPES = {"string": str, "integer": int, "boolean": bool, "array": list}


def validate(name: str, args: object) -> str | None:
    spec = next((t for t in TOOLS if t["name"] == name), None)
    if spec is None:
        return f"unknown tool {name!r}"
    if not isinstance(args, dict):
        return "input must be an object"
    if "__invalid_json__" in args:
        return "input was not valid JSON"
    schema = spec["input_schema"]
    for key in schema.get("required", []):
        if key not in args:
            return f"missing required field {key!r}"
    for key, value in args.items():
        prop = schema["properties"].get(key)
        if prop is None:
            return f"unexpected field {key!r}"
        expected = _TYPES[prop["type"]]
        if expected is int and isinstance(value, bool) or not isinstance(value, expected):
            return f"field {key!r} must be {prop['type']}"
    return None


def normalize_host(raw: str) -> str | None:
    host = raw.strip().lower().rstrip(".")
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    host = host.split("/", 1)[0].split(":", 1)[0]
    return host if HOST_RE.fullmatch(host) and len(host) <= 253 else None


@dataclass
class ToolOutcome:
    content: str
    is_error: bool
    summary: str  # short result for the UI, e.g. "exit 0 · 2.1s"
    detail: str = ""  # optional body for the UI (sanitized)


Emit = Callable[..., None]
ApproveNetwork = Callable[[list[str], str], Awaitable[list[str]]]


def describe(name: str, args: dict) -> str:
    """One-line description of a call for the UI header."""
    if not isinstance(args, dict):
        return ""
    match name:
        case "bash":
            return one_line(str(args.get("command", "")), 200)
        case "read_file":
            rng = ""
            if args.get("offset") or args.get("limit"):
                start = args.get("offset") or 1
                rng = f"  lines {start}–{start + (args.get('limit') or 2000) - 1}"
            return one_line(str(args.get("path", "")), 160) + rng
        case "write_file" | "edit_file":
            return one_line(str(args.get("path", "")), 160)
        case "search":
            where = args.get("glob") or args.get("path") or ""
            return one_line(f"{args.get('pattern', '')}  {where}".strip(), 160)
        case "list_files":
            return one_line(str(args.get("pattern") or args.get("path") or "."), 160)
        case "request_network":
            return one_line(", ".join(map(str, args.get("hosts", []))), 160)
    return ""


class Toolbox:
    def __init__(self, cell: Cell, emit: Emit, approve_network: ApproveNetwork):
        self.cell = cell
        self.emit = emit
        self.approve_network = approve_network
        self.current_job: str | None = None

    async def cancel_running(self) -> None:
        if self.current_job:
            await self.cell.kill_job(self.current_job)

    async def run(self, call_id: str, name: str, args: object) -> ToolOutcome:
        problem = validate(name, args)
        if problem:
            return ToolOutcome(f"Invalid tool call: {problem}", True, "invalid input")
        assert isinstance(args, dict)
        handler = getattr(self, f"_{name}")
        return await handler(call_id, **args)

    async def _bash(self, call_id: str, command: str, timeout_s: int = DEFAULT_TIMEOUT) -> ToolOutcome:
        timeout = max(1, min(int(timeout_s), MAX_TIMEOUT))
        job = secrets.token_hex(6)
        self.current_job = job

        def on_output(chunk: str) -> None:
            self.emit("tool.output", call_id=call_id, chunk=sanitize(chunk)[-8000:])

        try:
            r = await self.cell.exec(command, timeout=timeout, job=job, on_output=on_output)
        finally:
            self.current_job = None
        output = sanitize(r.output)
        if r.total_bytes > len(r.output.encode()):
            output += f"\n… [{r.total_bytes - len(r.output.encode())} more bytes not captured]"
        status = f"timed out after {timeout}s" if r.timed_out else f"exit {r.exit_code}"
        body = truncate_middle(output, MAX_RESULT)
        return ToolOutcome(f"[{status}]\n{body}" if body else f"[{status}] (no output)",
                           r.exit_code != 0, f"{status} · {r.duration:.1f}s")

    async def _fs(self, request: dict) -> dict:
        return await self.cell.fsop(request)

    async def _read_file(self, call_id: str, path: str, offset: int = 1, limit: int = 2000) -> ToolOutcome:
        r = await self._fs({"op": "read", "path": path, "offset": offset, "limit": limit})
        if not r.get("ok"):
            return ToolOutcome(sanitize(str(r.get("error"))), True, "error")
        content = sanitize(r["content"])
        total, start, end = r["total_lines"], r["start"], r["end"]
        note = "" if end >= total else f"\n[lines {start}–{end} of {total}; use offset to read more]"
        return ToolOutcome((content or "(empty file)") + note, False, f"{max(0, end - start + 1)} of {total} lines")

    async def _write_file(self, call_id: str, path: str, content: str) -> ToolOutcome:
        r = await self._fs({"op": "write", "path": path, "content": content})
        if not r.get("ok"):
            return ToolOutcome(sanitize(str(r.get("error"))), True, "error")
        verb = "created" if r["created"] else "overwrote"
        preview = truncate_middle(sanitize(content), 4000)
        return ToolOutcome(f"{verb} {path} ({r['lines']} lines)", False, f"{verb} · {r['lines']} lines", preview)

    async def _edit_file(self, call_id: str, path: str, old_string: str, new_string: str,
                         replace_all: bool = False) -> ToolOutcome:
        r = await self._fs({"op": "edit", "path": path, "old_string": old_string, "new_string": new_string,
                            "replace_all": replace_all})
        if not r.get("ok"):
            return ToolOutcome(sanitize(str(r.get("error"))), True, "error")
        n = r["replacements"]
        snippet = sanitize(r.get("snippet", ""))
        removed = "\n".join("- " + line for line in old_string.splitlines()[:40])
        added = "\n".join("+ " + line for line in new_string.splitlines()[:40])
        return ToolOutcome(
            f"Edited {path} ({n} replacement{'s' if n != 1 else ''}) at line {r['line']}:\n{snippet}",
            False,
            f"{n} replacement{'s' if n != 1 else ''} · line {r['line']}",
            sanitize(f"{removed}\n{added}"),
        )

    async def _search(self, call_id: str, pattern: str, path: str = ".", glob: str | None = None,
                      ignore_case: bool = False) -> ToolOutcome:
        r = await self._fs({"op": "search", "pattern": pattern, "path": path, "glob": glob,
                            "ignore_case": ignore_case})
        if not r.get("ok"):
            return ToolOutcome(sanitize(str(r.get("error"))), True, "error")
        lines = [sanitize(m) for m in r["matches"]]
        more = f"\n[{r['total'] - len(lines)} more matches not shown]" if r["truncated"] else ""
        return ToolOutcome("\n".join(lines) + more if lines else "No matches.", False,
                           f"{r['total']} match{'es' if r['total'] != 1 else ''}")

    async def _list_files(self, call_id: str, path: str = ".", pattern: str | None = None) -> ToolOutcome:
        r = await self._fs({"op": "list", "path": path, "pattern": pattern})
        if not r.get("ok"):
            return ToolOutcome(sanitize(str(r.get("error"))), True, "error")
        files = [sanitize(f) for f in r["files"]]
        more = f"\n[{r['total'] - len(files)} more files not shown]" if r["truncated"] else ""
        return ToolOutcome("\n".join(files) + more if files else "No files.", False, f"{r['total']} files")

    async def _request_network(self, call_id: str, hosts: list[str], reason: str) -> ToolOutcome:
        clean, bad = [], []
        for h in hosts[:20]:
            n = normalize_host(str(h))
            (clean if n else bad).append(n or one_line(str(h), 60))
        if not clean:
            return ToolOutcome(f"No valid host names given ({', '.join(bad)}).", True, "invalid hosts")
        allowed = await self.approve_network(clean, reason)
        denied = [h for h in clean if h not in allowed]
        parts = []
        if allowed:
            parts.append("Allowed: " + ", ".join(allowed))
        if denied:
            parts.append("Denied by the user: " + ", ".join(denied) + ". Don't ask for these again in this task.")
        if bad:
            parts.append("Ignored invalid names: " + ", ".join(bad))
        summary = "allowed " + ", ".join(allowed) if allowed else "denied"
        return ToolOutcome(". ".join(parts), not allowed, summary)
