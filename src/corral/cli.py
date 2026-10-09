"""corral command line."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, audit, keys, paths
from .client import GatewayClient, GatewayError
from .text import sanitize

SUBCOMMANDS = {"run", "attach", "review", "sessions", "daemon", "key", "doctor", "audit", "gc", "version"}


def repo_root(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    r = subprocess.run(["git", "-C", str(p if p.is_dir() else p.parent), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 else p


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in SUBCOMMANDS and argv[0] not in ("-h", "--help"):
        return tui_main(argv)
    if argv[0] in ("-h", "--help"):
        print(HELP)
        return
    cmd, rest = argv[0], argv[1:]
    handler = {
        "run": cmd_run, "attach": cmd_attach, "review": cmd_review, "sessions": cmd_sessions,
        "daemon": cmd_daemon, "key": cmd_key, "doctor": cmd_doctor, "audit": cmd_audit, "gc": cmd_gc,
        "version": lambda a: print(f"corral {__version__}"),
    }[cmd]
    sys.exit(handler(rest) or 0)


HELP = f"""corral {__version__}: a coding agent that works inside a sealed, single-use cell.

  corral [path] [--model M] [--new]   open the TUI for the repo at path (default: here)
  corral run "task" [--allow HOST]    run one task without the TUI; changes wait for review
  corral review SID                   review and apply a session's changes
  corral attach SID                   reopen a running session
  corral sessions [--all]             list sessions
  corral key set|rm|status [PROVIDER] manage API keys in the macOS Keychain
  corral doctor                       check Docker, the cell image, keys and the gateway
  corral daemon start|stop|restart|status|run
  corral audit verify [SID]           check audit log hash chains
  corral gc                           remove leftover cells
"""


# ---------------------------------------------------------------- tui


def _after_tui(sid: str | None) -> None:
    if sid:
        print(f"Session {sid} is still running in the gateway. Run `corral` in this repo to pick it up, "
              f"or `corral review {sid}` to apply its changes. Idle sessions close after 30 minutes.")


def tui_main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="corral", add_help=False)
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--model")
    ap.add_argument("--new", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    a = ap.parse_args(argv)
    if a.help:
        print(HELP)
        return
    from .tui.app import CorralApp

    _after_tui(CorralApp(repo_root(a.path), model=a.model, new=a.new).run())


def cmd_attach(argv: list[str]) -> int:
    if not argv:
        print("usage: corral attach SID", file=sys.stderr)
        return 2
    from .tui.app import CorralApp

    _after_tui(CorralApp(Path.cwd(), attach=argv[0]).run())
    return 0


def cmd_review(argv: list[str]) -> int:
    if not argv:
        print("usage: corral review SID", file=sys.stderr)
        return 2
    from .tui.app import CorralApp

    CorralApp(Path.cwd(), review=argv[0]).run()
    return 0


# ---------------------------------------------------------------- headless


def cmd_run(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="corral run")
    ap.add_argument("task")
    ap.add_argument("--path", default=".")
    ap.add_argument("--model")
    ap.add_argument("--allow", action="append", default=[], metavar="HOST",
                    help="allow HTTPS to HOST for this run (repeatable)")
    a = ap.parse_args(argv)
    return asyncio.run(_run(a.task, repo_root(a.path), a.model, a.allow))


async def _run(task: str, repo: Path, model: str | None, allow: list[str]) -> int:
    dim, gate, bad, reset = ("\x1b[2m", "\x1b[33m", "\x1b[31m", "\x1b[0m") if sys.stdout.isatty() else ("",) * 4
    c = GatewayClient()
    q: asyncio.Queue[dict] = asyncio.Queue()
    c.on_event = q.put_nowait
    try:
        await c.connect()
        info = await c.request("session.open", repo=str(repo), **({"model": model} if model else {}))
    except GatewayError as e:
        print(f"{bad}{e}{reset}", file=sys.stderr)
        return 1
    sid = info["sid"]
    print(f"{dim}session {sid} · {info['model']} · {repo}{reset}")
    if allow:
        await c.request("network.set", sid=sid, hosts=allow)
    await c.request("message.send", sid=sid, text=task)
    status = 0
    in_text = False
    while True:
        ev = await q.get()
        if ev.get("sid") != sid:
            continue
        kind = ev["event"]
        if kind != "text.delta" and in_text:
            print()
            in_text = False
        if kind == "log":
            print(f"{dim}{ev['kind']:<10}{sanitize(ev['text'])}{reset}")
        elif kind == "text.delta":
            print(sanitize(ev["text"]), end="", flush=True)
            in_text = True
        elif kind == "tool.start":
            print(f"{dim}▸ {ev['name']}  {sanitize(ev.get('title', ''))}{reset}")
        elif kind == "tool.end":
            print(f"{dim}  {sanitize(ev.get('summary', ''))}{reset}" if ev["ok"] else
                  f"{bad}  {sanitize(ev.get('summary', ''))}{reset}")
        elif kind == "approval.request":
            hosts = ", ".join(ev["hosts"])
            print(f"{gate}network request denied (headless): {hosts}. Re-run with --allow to permit.{reset}")
            await c.request("approval.respond", sid=sid, approval_id=ev["id"], allowed=[])
        elif kind == "network.event" and ev["decision"] == "deny":
            print(f"{gate}blocked   {ev['host']}: {ev['reason']}{reset}")
        elif kind == "notice":
            print(f"{bad if ev.get('level') == 'error' else gate}{sanitize(ev['text'])}{reset}")
            if ev.get("level") == "error":
                status = 1
        elif kind == "session.state" and ev["state"] == "error":
            print(f"{bad}session failed: {ev.get('error')}{reset}", file=sys.stderr)
            return 1
        elif kind == "turn.end":
            if ev.get("stop") not in ("end_turn", "stop"):
                status = status or 1
            break
    ch = await c.request("changes.get", sid=sid)
    files = [f for f in ch["files"] if f["state"] != "applied"]
    if files:
        print(f"\n{len(files)} file{'s' if len(files) != 1 else ''} changed in the cell:")
        for f in files:
            risk = f"  {gate}{', '.join(f['risks'])}{reset}" if f["risks"] else ""
            print(f"  {f['status']} {f['path']}  +{f['adds']} −{f['dels']}{risk}")
        print(f"\nNothing has touched your repo. Review and apply: corral review {sid}")
    else:
        print(f"\n{dim}No changes.{reset}")
    await c.request("session.close", sid=sid)
    await c.close()
    return status


# ---------------------------------------------------------------- sessions


def cmd_sessions(argv: list[str]) -> int:
    async def go():
        c = GatewayClient()
        await c.connect()
        rows = await c.request("session.list", all="--all" in argv)
        await c.close()
        return rows

    try:
        rows = asyncio.run(go())
    except GatewayError as e:
        print(e, file=sys.stderr)
        return 1
    if not rows:
        print("No sessions." if "--all" in argv else "No running sessions. --all includes closed ones.")
    for s in rows:
        age = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["created"]))
        print(f"{s['sid']}  {s['state']:<8} {age}  {Path(s['repo']).name:<20} {s.get('title') or ''}")
    return 0


# ---------------------------------------------------------------- daemon


def cmd_daemon(argv: list[str]) -> int:
    action = argv[0] if argv else "status"
    if action == "run":
        return _daemon_run()
    if action == "start":
        async def start():
            c = GatewayClient()
            await c.connect(autostart=True)
            await c.close()
        asyncio.run(start())
        print(f"gateway running (log: {paths.daemon_log_path()})")
        return 0
    if action in ("stop", "restart"):
        async def stop():
            c = GatewayClient()
            try:
                await c.connect(autostart=False)
            except GatewayError:
                return False
            await c.request("gateway.stop")
            await c.close()
            return True
        stopped = asyncio.run(stop())
        if stopped:
            for _ in range(100):
                if not paths.socket_path().exists():
                    break
                time.sleep(0.1)
        print("gateway stopped" if stopped else "gateway wasn't running")
        if action == "restart":
            return cmd_daemon(["start"])
        return 0
    if action == "status":
        async def status():
            c = GatewayClient()
            await c.connect(autostart=False)
            s = await c.request("gateway.status")
            await c.close()
            return s
        try:
            s = asyncio.run(status())
        except GatewayError as e:
            print(e)
            return 1
        pool = s["pool"]
        print(f"gateway {s['version']}  pid {s['pid']}  backend {s['backend']}")
        if s.get("backend_error"):
            print(f"  backend error: {s['backend_error']}")
        print(f"  warm cells {pool['ready']}/{pool['size']}" + (f"  ({pool['error']})" if pool.get("error") else ""))
        for i in s["sessions"]:
            print(f"  {i['sid']}  {i['state']:<8} {Path(i['repo']).name}  {i.get('title') or ''}")
        return 0
    print("usage: corral daemon start|stop|restart|status|run", file=sys.stderr)
    return 2


def _daemon_run() -> int:
    from .backend.cells import DockerRuntime, LocalRuntime
    from .config import load
    from .gateway.server import Gateway

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load()
    if os.environ.get("CORRAL_UNSAFE_LOCAL_CELLS") == "1":
        logging.warning("CORRAL_UNSAFE_LOCAL_CELLS=1: commands run directly on this machine with NO isolation.")
        runtime = LocalRuntime()
    else:
        runtime = DockerRuntime(cfg.cell)
    try:
        asyncio.run(Gateway(cfg, runtime).serve())
    except Exception as e:
        logging.error("gateway stopped: %s", e)
        return 1
    return 0


# ---------------------------------------------------------------- keys


def cmd_key(argv: list[str]) -> int:
    action = argv[0] if argv else "status"
    provider = argv[1] if len(argv) > 1 else None
    if action == "status":
        for p in sorted(keys.ENV_VARS):
            print(f"{p:<11} {keys.source(p)}")
        return 0
    if action in ("set", "rm") and not provider:
        print(f"usage: corral key {action} PROVIDER   ({', '.join(sorted(keys.ENV_VARS))})", file=sys.stderr)
        return 2
    if action == "set":
        print(f"Paste your {provider} key at the prompt. It goes straight into the macOS Keychain.")
        rc = keys.keychain_set_interactive(provider)
        if rc == 0:
            print("Saved. Restart the gateway to use it: corral daemon restart")
        return rc
    if action == "rm":
        return keys.keychain_delete(provider)
    print("usage: corral key set|rm|status [PROVIDER]", file=sys.stderr)
    return 2


# ---------------------------------------------------------------- doctor, audit, gc


def cmd_doctor(argv: list[str]) -> int:
    from .backend.cells import DockerRuntime
    from .config import load

    cfg = load()
    rt = DockerRuntime(cfg.cell)
    ok_all = True

    def line(ok: bool | None, what: str, detail: str = "") -> None:
        nonlocal ok_all
        mark = {True: "ok     ", False: "problem", None: "note   "}[ok]
        if ok is False:
            ok_all = False
        print(f"{mark}  {what:<14} {detail}")

    async def checks():
        problem = await rt.check()
        line(problem is None, "docker", problem or "running")
        if problem is None:
            present = await rt.image_present()
            line(present, "cell image", cfg.cell.image if present else
                 f"{cfg.cell.image} not built yet; the gateway builds it on first use")
        home = paths.home()
        mode = oct(home.stat().st_mode & 0o777)
        line(mode == "0o700", "state dir", f"{home} {mode}")
        c = GatewayClient()
        try:
            await c.connect(autostart=False)
            s = await c.request("gateway.status")
            line(True, "gateway", f"pid {s['pid']}, backend {s['backend']}, "
                                  f"{s['pool']['ready']}/{s['pool']['size']} warm cells")
            await c.close()
        except GatewayError:
            line(None, "gateway", "not running; starts on first use")
        for p in ("anthropic", "openai", "openrouter"):
            src = keys.source(p)
            line(None if src == "none" else True, f"key {p}", src)
        try:
            import httpx2
            async with httpx2.AsyncClient(timeout=2) as h:
                r = await h.get("http://localhost:11434/api/tags")
                names = [m["name"] for m in r.json().get("models", [])]
            line(True, "ollama", ", ".join(names[:5]) or "running, no models")
        except Exception:
            line(None, "ollama", "not reachable at localhost:11434")
        line(None, "model", f"{cfg.model} (config: {paths.config_path()})")

    asyncio.run(checks())
    return 0 if ok_all else 1


def cmd_audit(argv: list[str]) -> int:
    if not argv or argv[0] != "verify":
        print("usage: corral audit verify [SID]", file=sys.stderr)
        return 2
    files = [paths.audit_dir() / f"{argv[1]}.jsonl"] if len(argv) > 1 else sorted(paths.audit_dir().glob("*.jsonl"))
    bad = 0
    for f in files:
        if not f.exists():
            print(f"missing  {f.name}")
            bad += 1
            continue
        r = audit.verify(f)
        print(f"{'ok     ' if r.ok else 'BROKEN '}  {f.stem}  {r.records} records" + (f"  {r.error}" if r.error else ""))
        bad += not r.ok
    return 1 if bad else 0


def cmd_gc(argv: list[str]) -> int:
    from .backend.cells import DockerRuntime
    from .config import load

    async def go():
        c = GatewayClient()
        try:
            await c.connect(autostart=False)
            print("The gateway is running; it cleans up its own cells. Stop it first: corral daemon stop")
            await c.close()
            return 1
        except GatewayError:
            pass
        n = await DockerRuntime(load().cell).gc()
        print(f"removed {n} leftover container{'s' if n != 1 else ''}")
        return 0

    return asyncio.run(go())
