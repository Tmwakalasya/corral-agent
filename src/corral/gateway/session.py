"""One agent session: a repo snapshot, one cell, one conversation.

The session owns the boundary crossings. The agent can only ask; the session
decides, and every decision lands in the audit log:

- network: hosts reach the proxy allowlist only after the user approves
- changes: work leaves the cell as a git bundle, is checked by the host's git,
  and reaches the user's repo only through ``apply`` for files they chose
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .. import paths
from ..agent import prompts
from ..agent.loop import Agent
from ..agent.tools import Toolbox, normalize_host
from ..audit import AuditLog
from ..backend import mirror
from ..backend.cells import Cell
from ..text import one_line, sanitize, truncate_middle

if TYPE_CHECKING:
    from .server import Connection, Gateway

MAX_EVENTS = 20_000
NOTES_FILES = ("AGENTS.md", "CLAUDE.md")


class GatewayError(Exception):
    pass


def _load_trust() -> dict:
    try:
        return json.loads(paths.trust_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_trust(data: dict) -> None:
    p = paths.trust_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.chmod(0o600)
    tmp.replace(p)


class Session:
    def __init__(self, gw: Gateway, sid: str, repo: Path, model: str):
        self.gw = gw
        self.sid = sid
        self.repo = repo
        self.mirror = paths.mirror_for(repo)
        self.model = model
        self.created = time.time()
        self.state = "starting"
        self.error: str | None = None
        self.title = ""
        self.events: list[dict] = []
        self.seq = 0
        self.subscribers: set[Connection] = set()
        self.audit = AuditLog(paths.audit_dir() / f"{sid}.jsonl")
        self.cell: Cell | None = None
        self.base: str | None = None
        self.work: str | None = None
        self.allow: set[str] = set(gw.cfg.default_allow)
        self.ready = asyncio.Event()
        self.turn_task: asyncio.Task | None = None
        self.start_task: asyncio.Task | None = None
        self.net_task: asyncio.Task | None = None
        self.approvals: dict[str, tuple[asyncio.Future, list[str]]] = {}
        self.agent: Agent | None = None
        self.files: list[dict] = []
        self.withheld: list[str] = []
        self.notes_pending: tuple[str, str, str] | None = None  # (file, sha, text)
        self.sync_lock = asyncio.Lock()
        self.budget = gw.cfg.budget_usd
        self.last_activity = time.time()

    # ------------------------------------------------------------ events

    def emit(self, event: str, **data) -> None:
        self.last_activity = time.time()
        last = self.events[-1] if self.events else None
        if event in ("text.delta", "note.delta", "tool.output") and last and last["event"] == event \
                and last.get("call_id") == data.get("call_id"):
            key = "chunk" if event == "tool.output" else "text"
            last[key] = (last[key] + data[key])[-200_000:]
        else:
            self.seq += 1
            self.events.append({"event": event, "sid": self.sid, "seq": self.seq, **data})
            if len(self.events) > MAX_EVENTS:
                del self.events[: len(self.events) - MAX_EVENTS]
        msg = {"event": event, "sid": self.sid, "seq": self.seq, **data}
        for conn in list(self.subscribers):
            conn.send(msg)

    def log(self, kind: str, text: str, seconds: float | None = None) -> None:
        self.emit("log", kind=kind, text=text, seconds=None if seconds is None else round(seconds, 2))

    def info(self) -> dict:
        return {
            "sid": self.sid, "repo": str(self.repo), "model": self.model, "state": self.state,
            "error": self.error, "created": self.created, "title": self.title,
            "files": len([f for f in self.files if f["state"] != "applied"]),
            "cell": self.cell.id if self.cell else None,
            "cell_kind": self.cell.kind if self.cell else None,
            "hosts": sorted(self.allow), "budget": self.budget,
            "stats": self.agent.stats() if self.agent else None,
            "pending_approvals": [{"id": k, "hosts": v[1]} for k, v in self.approvals.items()],
        }

    def _persist(self) -> None:
        meta = {"sid": self.sid, "repo": str(self.repo), "mirror": str(self.mirror), "base": self.base,
                "work": self.work, "model": self.model, "created": self.created, "title": self.title,
                "state": self.state}
        p = paths.sessions_dir() / f"{self.sid}.json"
        p.write_text(json.dumps(meta))
        p.chmod(0o600)

    @classmethod
    def from_meta(cls, gw: Gateway, meta: dict) -> Session:
        s = cls(gw, meta["sid"], Path(meta["repo"]), meta["model"])
        s.created, s.title = meta["created"], meta.get("title", "")
        s.base, s.work = meta.get("base"), meta.get("work")
        s.state = "closed"
        s.ready.set()
        return s

    # ------------------------------------------------------------ lifecycle

    def begin(self) -> None:
        self.start_task = asyncio.create_task(self._start(), name=f"session-{self.sid}-start")

    async def _start(self) -> None:
        cfg = self.gw.cfg
        try:
            self.audit.write("session.start", repo=str(self.repo), model=self.model)
            self.emit("session.state", state="starting")
            await self.gw.wait_ready(self)

            t = time.monotonic()
            snap = await mirror.snapshot(self.repo, self.mirror, self.sid, history=cfg.cell.history)
            self.base = self.work = snap.base
            self.withheld = snap.withheld
            self.log("snapshot", f"{snap.files} files, baseline {snap.base[:10]}", time.monotonic() - t)
            if snap.withheld:
                self.log("withheld", ", ".join(snap.withheld[:12]) + (" …" if len(snap.withheld) > 12 else ""))
            if snap.skipped:
                self.log("skipped", "; ".join(snap.skipped[:6]) + (" …" if len(snap.skipped) > 6 else ""))
            self.audit.write("snapshot", base=snap.base, head=snap.head, files=snap.files, withheld=snap.withheld)

            cell, warm, waited = await self.gw.pool.acquire()
            self.cell = cell
            self.log("cell", f"{cell.id} ({'warm pool' if warm else 'cold start'})", waited)

            t = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="corral-b-") as tmp:
                bundle = Path(tmp) / "in.bundle"
                await mirror.bundle_for_cell(self.mirror, snap, bundle)
                await cell.materialize(bundle, snap.ref)
            self.log("checkout", "/workspace ready", time.monotonic() - t)

            await cell.set_allowlist(sorted(self.allow))
            self.log("network", ", ".join(sorted(self.allow)) if self.allow else "no hosts allowed")
            self.net_task = asyncio.create_task(self._watch_network(), name=f"session-{self.sid}-net")

            notes = self._project_notes()
            system = prompts.build(self.repo.name, snap.base, notes, snap.withheld)
            toolbox = Toolbox(cell, self.emit, self.approve_network)
            self.agent = Agent(cfg=cfg, router=self.gw.router, model=self.model, system=system, toolbox=toolbox,
                               emit=self.emit, audit=self.audit, budget_usd=self.budget)
            self.state = "ready"
            self._persist()
            self.audit.write("cell", id=cell.id, kind=cell.kind, warm=warm)
            self.emit("session.state", state="ready", cell=cell.id, cell_kind=cell.kind, base=snap.base,
                      hosts=sorted(self.allow))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.state = "error"
            self.error = str(e)
            self.audit.write("session.error", error=str(e))
            self.emit("session.state", state="error", error=str(e))
            if self.cell:
                await self.gw.pool.release(self.cell)
                self.cell = None
        finally:
            self.ready.set()

    def _project_notes(self) -> str | None:
        for name in NOTES_FILES:
            p = self.repo / name
            if not p.is_file() or p.is_symlink():
                continue
            text = p.read_text(errors="replace")[:20_000]
            sha = hashlib.sha256(text.encode()).hexdigest()
            trusted = _load_trust().get(str(self.repo), {})
            if trusted.get(name) == sha:
                self.log("notes", f"{name} loaded (trusted)")
                return text
            self.notes_pending = (name, sha, text)
            self.emit("trust.needed", file=name, sha=sha, text=truncate_middle(sanitize(text), 8000))
            return None
        return None

    async def trust_notes(self) -> str:
        if not self.notes_pending:
            raise GatewayError("Nothing to trust in this session.")
        name, sha, text = self.notes_pending
        data = _load_trust()
        data.setdefault(str(self.repo), {})[name] = sha
        _save_trust(data)
        self.notes_pending = None
        self.audit.write("trust", file=name, sha=sha)
        if self.agent and not self.agent.messages and self.base:
            self.agent.system = prompts.build(self.repo.name, self.base, text, self.withheld)
            msg = f"{name} trusted and loaded for this session."
        else:
            msg = f"{name} trusted. It loads from your next session."
        self.emit("notice", level="info", text=msg)
        return msg

    async def close(self) -> None:
        if self.state == "closed":
            return
        if self.start_task and not self.start_task.done():
            self.start_task.cancel()
        await self.cancel()
        if self.cell and self.base:
            try:
                await self.sync_changes()
            except Exception as e:
                self.emit("notice", level="warn", text=f"Couldn't collect final changes: {e}")
        if self.net_task:
            self.net_task.cancel()
        if self.cell:
            await self.gw.pool.release(self.cell)
            self.cell = None
        self.state = "closed"
        self.agent = None
        self._persist()
        self.audit.write("session.close")
        self.emit("session.state", state="closed")

    # ------------------------------------------------------------ turns

    async def send(self, text: str) -> None:
        if self.state in ("closed", "error"):
            raise GatewayError(f"Session is {self.state}.")
        if self.turn_task and not self.turn_task.done():
            raise GatewayError("The agent is still working. Press Esc to stop it first.")
        text = text.strip()
        if not text:
            raise GatewayError("Empty message.")
        if not self.title:
            self.title = one_line(text, 80)
        self.emit("user", text=text)
        self.turn_task = asyncio.create_task(self._turn(text), name=f"session-{self.sid}-turn")

    async def _turn(self, text: str) -> None:
        await self.ready.wait()
        if self.state != "ready" or not self.agent:
            self.emit("notice", level="error", text="The cell isn't available, so the message wasn't sent.")
            return
        self.state = "running"
        self.emit("session.state", state="running")
        try:
            await self.agent.run_turn(text)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.emit("notice", level="error", text=f"Turn failed: {e}")
        finally:
            if self.state == "running":
                self.state = "ready"
                self.emit("session.state", state="ready")
        try:
            await self.sync_changes()
        except Exception as e:
            self.emit("notice", level="warn", text=f"Couldn't collect changes from the cell: {e}")

    async def cancel(self) -> None:
        for fut, _ in list(self.approvals.values()):
            if not fut.done():
                fut.set_result([])
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
            try:
                await self.turn_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------ changes

    async def sync_changes(self) -> list[dict]:
        async with self.sync_lock:
            if self.cell and self.base and self.state != "running":
                with tempfile.TemporaryDirectory(prefix="corral-o-") as tmp:
                    out = Path(tmp) / "out.bundle"
                    changed = await self.cell.extract(self.base, out, self.gw.cfg.cell.max_bundle_mb * 1024 * 1024)
                    if changed:
                        self.work = await mirror.receive_bundle(self.mirror, self.sid, out)
                    else:
                        self.work = self.base
                        await mirror.set_work(self.mirror, self.sid, self.base)
                self._persist()
            if self.base and self.work:
                files = await mirror.changes(self.repo, self.mirror, self.base, self.work)
                self.files = [f.to_dict() for f in files]
            self.emit("changes", files=self.files)
            return self.files

    async def diff(self, path: str) -> str:
        if not (self.base and self.work):
            return ""
        text = await mirror.file_diff(self.mirror, self.base, self.work, path)
        return truncate_middle(sanitize(text), 400_000)

    async def apply(self, selected: list[str]) -> dict:
        if not (self.base and self.work):
            raise GatewayError("No changes to apply yet.")
        result = await mirror.apply(self.repo, self.mirror, self.base, self.work, selected)
        self.audit.write("apply", base=self.base, work=self.work, applied=result.applied, refused=result.refused)
        files = await mirror.changes(self.repo, self.mirror, self.base, self.work)
        self.files = [f.to_dict() for f in files]
        self.emit("changes", files=self.files)
        self.emit("applied", applied=result.applied, refused=[{"path": p, "reason": r} for p, r in result.refused])
        return {"applied": result.applied, "refused": [{"path": p, "reason": r} for p, r in result.refused]}

    # ------------------------------------------------------------ network

    async def approve_network(self, hosts: list[str], reason: str) -> list[str]:
        new = [h for h in hosts if h not in self.allow]
        if new:
            aid = secrets.token_hex(4)
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self.approvals[aid] = (fut, new)
            self.audit.write("approval.request", id=aid, hosts=new, reason=reason)
            self.emit("approval.request", id=aid, kind="network", hosts=new, reason=one_line(reason, 400))
            try:
                answer = await fut
            finally:
                self.approvals.pop(aid, None)
            granted = [h for h in answer if h in new]
            self.audit.write("approval.result", id=aid, allowed=granted)
            self.emit("approval.resolved", id=aid, allowed=granted)
            if granted:
                await self.set_hosts(self.allow | set(granted))
        return [h for h in hosts if h in self.allow]

    def respond(self, aid: str, allowed: list[str]) -> None:
        entry = self.approvals.get(aid)
        if not entry:
            raise GatewayError("That request was already answered.")
        fut, _ = entry
        if not fut.done():
            fut.set_result([h for h in allowed if isinstance(h, str)])

    async def set_hosts(self, hosts: set[str]) -> list[str]:
        clean = set()
        for h in hosts:
            n = normalize_host(h)
            if not n:
                raise GatewayError(f"Not a host name: {one_line(h, 60)}")
            clean.add(n)
        self.allow = clean
        if self.cell:
            await self.cell.set_allowlist(sorted(clean))
        self.audit.write("network.allowlist", hosts=sorted(clean))
        self.emit("network.allowlist", hosts=sorted(clean))
        return sorted(clean)

    async def _watch_network(self) -> None:
        assert self.cell
        async for ev in self.cell.network_events():
            entry = {
                "decision": one_line(str(ev.get("decision", "")), 10),
                "host": one_line(str(ev.get("host", "")), 120),
                "port": ev.get("port") if isinstance(ev.get("port"), int) else 0,
                "reason": one_line(str(ev.get("reason", "")), 160),
                "up": ev.get("bytes_up") if isinstance(ev.get("bytes_up"), int) else 0,
                "down": ev.get("bytes_down") if isinstance(ev.get("bytes_down"), int) else 0,
            }
            self.audit.write("net", **entry)
            self.emit("network.event", **entry)
