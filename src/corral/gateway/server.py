"""The gateway daemon: GitFarm's gateway + backend roles on one machine.

- Gateway: the only process that holds API keys, talks to models, enforces
  budgets and approvals, and writes the audit log. Clients (the TUI, `corral
  run`) connect over a unix socket that only this user can open, and do
  nothing but render and relay the user's decisions.
- Backend: keeps one bare mirror per repo and a warm pool of fresh cells, and
  admits a bounded number of concurrent sessions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
import socket
import struct
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .. import __version__, paths, protocol
from ..agent.models import ProviderError, Router
from ..backend import mirror
from ..backend.pool import WarmPool
from ..config import Config
from .session import GatewayError, Session

log = logging.getLogger("corral.gateway")


def peer_uid(sock: socket.socket) -> int | None:
    try:
        if sys.platform == "darwin":
            # LOCAL_PEERCRED → struct xucred { u_int cr_version; uid_t cr_uid; ... }
            raw = sock.getsockopt(0, 0x001, 76)
            return struct.unpack_from("=Ii", raw, 0)[1]
        if sys.platform.startswith("linux"):
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", raw)[1]
    except OSError:
        return None
    return None


class Connection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=50_000)
        self.subscriptions: set[Session] = set()
        self.closed = False

    def send(self, obj: dict) -> None:
        if self.closed:
            return
        try:
            self.queue.put_nowait(protocol.encode(obj))
        except asyncio.QueueFull:
            self.close()  # a client that stopped reading doesn't get to hold memory

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            for s in self.subscriptions:
                s.subscribers.discard(self)
            try:
                self.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def pump(self) -> None:
        try:
            while (data := await self.queue.get()) is not None:
                self.writer.write(data)
                await self.writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self.close()
            try:
                self.writer.close()
            except Exception:
                pass


class Gateway:
    def __init__(self, cfg: Config, runtime, socket_path: Path | None = None):
        self.cfg = cfg
        self.runtime = runtime
        self.socket_path = socket_path or paths.socket_path()
        self.sessions: dict[str, Session] = {}
        self.pool = WarmPool(runtime, cfg.pool_size)
        self.router = Router(cfg)
        self._prepare: asyncio.Task | None = None
        self._setup_listeners: set[Session] = set()
        self._stop = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None

    # ------------------------------------------------------------ setup

    def _setup_line(self, line: str) -> None:
        for s in list(self._setup_listeners):
            s.log("setup", line)

    async def _prepare_backend(self) -> None:
        problem = await self.runtime.check()
        if problem:
            raise GatewayError(problem)
        await self.runtime.gc(keep=self.pool.live)
        if not await self.runtime.image_present():
            self._setup_line("Building the cell image. First run only; takes a few minutes.")

            def on_line(line: str) -> None:
                if line.startswith("#") and ("DONE" in line or "RUN" in line or "FROM" in line):
                    self._setup_line(line[:160])

            await self.runtime.build_image(on_line)
            self._setup_line("Cell image built.")
        self.pool.start()

    def _ensure_prepare(self) -> asyncio.Task:
        if self._prepare is None or (self._prepare.done() and self._prepare.exception() is not None):
            self._prepare = asyncio.create_task(self._prepare_backend(), name="prepare")
        return self._prepare

    async def wait_ready(self, session: Session) -> None:
        self._setup_listeners.add(session)
        try:
            await asyncio.shield(self._ensure_prepare())
        finally:
            self._setup_listeners.discard(session)

    # ------------------------------------------------------------ serving

    async def serve(self) -> None:
        sock = self.socket_path
        if sock.exists():
            try:
                _, w = await asyncio.open_unix_connection(str(sock))
                w.close()
                raise GatewayError(f"A gateway is already running on {sock}.")
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                sock.unlink(missing_ok=True)
        old = os.umask(0o077)
        try:
            self._server = await asyncio.start_unix_server(self._handle, path=str(sock), limit=protocol.MAX_LINE)
        finally:
            os.umask(old)
        os.chmod(sock, 0o600)
        paths.pid_path().write_text(str(os.getpid()))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        self._ensure_prepare()
        reaper = asyncio.create_task(self._reap_idle(), name="reaper")
        log.info("gateway %s listening on %s", __version__, sock)
        try:
            await self._stop.wait()
        finally:
            reaper.cancel()
            await self.shutdown()

    async def _reap_idle(self) -> None:
        """Close sessions nobody is watching and that have done nothing for a while."""
        while True:
            await asyncio.sleep(60)
            limit = self.cfg.idle_close_minutes * 60
            for s in list(self.sessions.values()):
                if s.state in ("ready", "error") and not s.subscribers and time.time() - s.last_activity > limit:
                    log.info("closing idle session %s", s.sid)
                    await s.close()

    async def shutdown(self) -> None:
        for s in list(self.sessions.values()):
            try:
                await s.close()
            except Exception as e:
                log.warning("closing %s: %s", s.sid, e)
        if self._prepare and not self._prepare.done():
            self._prepare.cancel()
        await self.pool.close()
        await self.router.aclose()
        if self._server:
            self._server.close()
        self.socket_path.unlink(missing_ok=True)
        paths.pid_path().unlink(missing_ok=True)

    def stop(self) -> None:
        self._stop.set()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = writer.get_extra_info("socket")
        uid = peer_uid(raw) if raw is not None else None
        if uid is not None and uid != os.getuid():
            writer.close()
            return
        conn = Connection(reader, writer)
        pump = asyncio.create_task(conn.pump())
        try:
            while not conn.closed:
                msg = await protocol.read(reader)
                if msg is None:
                    break
                rid = msg.get("id")
                op = msg.get("op")
                try:
                    handler = self._ops().get(op) if isinstance(op, str) else None
                    if handler is None:
                        raise GatewayError(f"unknown op {op!r}")
                    result = await handler(conn, msg)
                    conn.send({"id": rid, "ok": True, "result": result})
                except (GatewayError, ProviderError) as e:
                    conn.send({"id": rid, "ok": False, "error": str(e)})
                except Exception as e:
                    log.exception("op %s failed", op)
                    conn.send({"id": rid, "ok": False, "error": f"internal error: {type(e).__name__}: {e}"})
        finally:
            conn.close()
            await pump

    # ------------------------------------------------------------ ops

    def _ops(self) -> dict[str, Callable]:
        return {
            "hello": self.op_hello,
            "session.open": self.op_open,
            "session.attach": self.op_attach,
            "session.list": self.op_list,
            "session.close": self.op_close,
            "message.send": self.op_send,
            "turn.cancel": self.op_cancel,
            "approval.respond": self.op_approval,
            "changes.get": self.op_changes,
            "changes.diff": self.op_diff,
            "changes.apply": self.op_apply,
            "network.set": self.op_network,
            "model.set": self.op_model,
            "budget.set": self.op_budget,
            "trust.accept": self.op_trust,
            "gateway.status": self.op_status,
            "gateway.stop": self.op_stop,
        }

    def _session(self, msg: dict) -> Session:
        sid = msg.get("sid")
        if not isinstance(sid, str):
            raise GatewayError("missing sid")
        if sid in self.sessions:
            return self.sessions[sid]
        meta_path = paths.sessions_dir() / f"{os.path.basename(sid)}.json"
        if meta_path.exists():
            s = Session.from_meta(self, json.loads(meta_path.read_text()))
            self.sessions[sid] = s
            return s
        raise GatewayError(f"no session {sid}")

    async def op_hello(self, conn, msg):
        return {"version": __version__, "protocol": protocol.VERSION, "pid": os.getpid()}

    async def op_open(self, conn, msg):
        repo_arg = msg.get("repo")
        if not isinstance(repo_arg, str):
            raise GatewayError("missing repo")
        repo = await mirror.is_git_repo(Path(repo_arg))
        if repo is None:
            raise GatewayError(f"{repo_arg} is not inside a git repository. Run `git init` there first.")
        active = [s for s in self.sessions.values() if s.state not in ("closed", "error")]
        if len(active) >= self.cfg.max_sessions:
            raise GatewayError(f"{len(active)} sessions are already running (limit {self.cfg.max_sessions}). "
                               "Close one first.")
        model = msg.get("model") or self.cfg.model
        self.router.split(model)
        sid = secrets.token_hex(4)
        s = Session(self, sid, repo, model)
        self.sessions[sid] = s
        s.subscribers.add(conn)
        conn.subscriptions.add(s)
        s.begin()
        return s.info()

    async def op_attach(self, conn, msg):
        s = self._session(msg)
        if msg.get("replay", True):
            for ev in s.events:
                conn.send(ev)
        s.subscribers.add(conn)
        conn.subscriptions.add(s)
        return s.info()

    async def op_list(self, conn, msg):
        out = [s.info() for s in self.sessions.values()]
        if msg.get("all"):
            known = set(self.sessions)
            for p in sorted(paths.sessions_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
                try:
                    meta = json.loads(p.read_text())
                except json.JSONDecodeError:
                    continue
                if meta.get("sid") not in known:
                    out.append({**meta, "state": "closed", "files": None, "stats": None})
        repo = msg.get("repo")
        if isinstance(repo, str):
            out = [i for i in out if i["repo"] == repo]
        return sorted(out, key=lambda i: i["created"], reverse=True)

    async def op_close(self, conn, msg):
        await self._session(msg).close()
        return {}

    async def op_send(self, conn, msg):
        text = msg.get("text")
        if not isinstance(text, str):
            raise GatewayError("missing text")
        await self._session(msg).send(text)
        return {}

    async def op_cancel(self, conn, msg):
        await self._session(msg).cancel()
        return {}

    async def op_approval(self, conn, msg):
        allowed = msg.get("allowed") or []
        if not isinstance(allowed, list):
            raise GatewayError("allowed must be a list")
        self._session(msg).respond(str(msg.get("approval_id")), allowed)
        return {}

    async def op_changes(self, conn, msg):
        s = self._session(msg)
        files = await s.sync_changes() if msg.get("sync", True) else s.files
        return {"files": files, "base": s.base, "work": s.work, "repo": str(s.repo), "state": s.state}

    async def op_diff(self, conn, msg):
        path = msg.get("path")
        if not isinstance(path, str):
            raise GatewayError("missing path")
        return {"diff": await self._session(msg).diff(path)}

    async def op_apply(self, conn, msg):
        selected = msg.get("paths")
        if not isinstance(selected, list) or not all(isinstance(p, str) for p in selected):
            raise GatewayError("paths must be a list of strings")
        return await self._session(msg).apply(selected)

    async def op_network(self, conn, msg):
        hosts = msg.get("hosts")
        if not isinstance(hosts, list):
            raise GatewayError("hosts must be a list")
        return {"hosts": await self._session(msg).set_hosts(set(map(str, hosts)))}

    async def op_model(self, conn, msg):
        s = self._session(msg)
        model = msg.get("model")
        if not isinstance(model, str) or not model:
            raise GatewayError("missing model")
        self.router.split(model)
        s.model = model
        if s.agent:
            s.agent.model = model
        s.audit.write("model.set", model=model)
        s.emit("model", model=model)
        return {"model": model}

    async def op_budget(self, conn, msg):
        s = self._session(msg)
        usd = float(msg.get("usd", 0))
        if usd <= 0:
            raise GatewayError("budget must be positive")
        s.budget = usd
        if s.agent:
            s.agent.budget_usd = usd
            s.emit("usage", **s.agent.stats())
        s.audit.write("budget.set", usd=usd)
        return {"budget": usd}

    async def op_trust(self, conn, msg):
        return {"message": await self._session(msg).trust_notes()}

    async def op_status(self, conn, msg):
        prep = self._prepare
        state = "pending" if prep is None or not prep.done() else ("error" if prep.exception() else "ready")
        return {
            "version": __version__, "pid": os.getpid(), "pool": self.pool.status(), "backend": state,
            "backend_error": str(prep.exception()) if prep and prep.done() and prep.exception() else None,
            "sessions": [s.info() for s in self.sessions.values() if s.state != "closed"],
        }

    async def op_stop(self, conn, msg):
        asyncio.get_running_loop().call_later(0.1, self._stop.set)
        return {}
