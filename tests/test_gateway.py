"""Gateway end to end over its unix socket, with a scripted model and the local cell."""

import asyncio
import os
import stat

from corral import audit, paths
from corral.backend.cells import LocalRuntime
from corral.client import GatewayClient, GatewayError
from corral.config import Config
from corral.gateway.server import Gateway

from .conftest import ScriptedProvider, tool_use

import pytest


async def wait_for(events, pred, timeout=20.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for e in events:
            if pred(e):
                return e
        await asyncio.sleep(0.02)
    raise AssertionError(f"event not seen; last: {events[-6:]}")


@pytest.fixture
async def gateway(home):
    cfg = Config(pool_size=1)
    gw = Gateway(cfg, LocalRuntime())
    task = asyncio.create_task(gw.serve())
    for _ in range(100):
        if paths.socket_path().exists():
            break
        await asyncio.sleep(0.02)
    yield gw
    gw.stop()
    await task


async def connect():
    c = GatewayClient()
    events: list[dict] = []
    c.on_event = events.append
    await c.connect(autostart=False)
    return c, events


async def test_socket_is_private(gateway):
    mode = stat.S_IMODE(os.stat(paths.socket_path()).st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(os.stat(paths.home()).st_mode) == 0o700


async def test_full_session(gateway, repo):
    provider = ScriptedProvider([
        lambda m: [
            {"type": "text", "text": "Fixing the sign."},
            tool_use("t1", "edit_file", path="app.py", old_string="a - b", new_string="a + b"),
            tool_use("t2", "bash", command="python3 -c 'import app; print(app.add(2, 3))'"),
        ],
        lambda m: [tool_use("t3", "request_network", hosts=["https://pypi.org/simple", "evil.example"],
                            reason="install pytest")],
        lambda m: [tool_use("t4", "read_file", path="nope.py")],
        lambda m: [{"type": "text", "text": "Done."}],
    ])
    gateway.router.resolve = lambda model: (provider, model)

    c, events = await connect()
    info = await c.request("session.open", repo=str(repo / "app.py"))  # a path inside the repo works too
    sid = info["sid"]
    await c.request("message.send", sid=sid, text="fix add")  # sent before the cell is ready; it waits
    await wait_for(events, lambda e: e["event"] == "session.state" and e["state"] == "ready")
    req = await wait_for(events, lambda e: e["event"] == "approval.request")
    assert req["hosts"] == ["pypi.org", "evil.example"] and req["reason"] == "install pytest"
    await c.request("approval.respond", sid=sid, approval_id=req["id"], allowed=["pypi.org"])
    await wait_for(events, lambda e: e["event"] == "turn.end")
    changes = await wait_for(events, lambda e: e["event"] == "changes" and e["files"])

    ends = {e["call_id"]: e for e in events if e["event"] == "tool.end"}
    assert ends["t1"]["ok"] and "1 replacement" in ends["t1"]["summary"]
    assert ends["t2"]["ok"] and ends["t2"]["summary"].startswith("exit 0")
    assert ends["t3"]["summary"] == "allowed pypi.org"
    assert not ends["t4"]["ok"]
    # The model saw the command's real output and the user's decision.
    results = [b for m in provider.seen[1] for b in m["content"] if b.get("type") == "tool_result"]
    assert "5" in results[1]["content"]
    results = [b for m in provider.seen[2] for b in m["content"] if b.get("type") == "tool_result"]
    assert "Allowed: pypi.org" in results[-1]["content"] and "evil.example" in results[-1]["content"]

    assert [f["path"] for f in changes["files"]] == ["app.py"]
    assert (repo / "app.py").read_text().endswith("a - b\n")  # nothing touched the host yet
    diff = await c.request("changes.diff", sid=sid, path="app.py")
    assert "+    return a + b" in diff["diff"]
    applied = await c.request("changes.apply", sid=sid, paths=["app.py"])
    assert applied["applied"] == ["app.py"]
    assert (repo / "app.py").read_text().endswith("a + b\n")

    s = await c.request("session.list")
    assert s[0]["hosts"] == ["pypi.org"]
    await c.request("session.close", sid=sid)
    await c.close()

    log = paths.audit_dir() / f"{sid}.jsonl"
    v = audit.verify(log)
    assert v.ok and v.records > 5
    kinds = [line for line in log.read_text().splitlines()]
    assert any('"approval.result"' in k for k in kinds) and any('"apply"' in k for k in kinds)


async def test_reattach_replays_and_review_after_close(gateway, repo):
    provider = ScriptedProvider([lambda m: [tool_use("w", "write_file", path="hello.txt", content="hi\n")]])
    gateway.router.resolve = lambda model: (provider, model)
    c, events = await connect()
    sid = (await c.request("session.open", repo=str(repo)))["sid"]
    await c.request("message.send", sid=sid, text="write hello")
    await wait_for(events, lambda e: e["event"] == "changes" and e["files"])
    await c.close()

    c2, events2 = await connect()
    await c2.request("session.attach", sid=sid)
    await wait_for(events2, lambda e: e["event"] == "tool.end" and e["call_id"] == "w")
    await c2.request("session.close", sid=sid)
    # Closed: cell gone, but the work is still reviewable from the mirror.
    gateway.sessions.clear()
    ch = await c2.request("changes.get", sid=sid)
    assert ch["state"] == "closed" and [f["path"] for f in ch["files"]] == ["hello.txt"]
    await c2.request("changes.apply", sid=sid, paths=["hello.txt"])
    assert (repo / "hello.txt").read_text() == "hi\n"
    await c2.close()


async def test_cancel_mid_command(gateway, repo):
    provider = ScriptedProvider([lambda m: [tool_use("s", "bash", command="sleep 30")]])
    gateway.router.resolve = lambda model: (provider, model)
    c, events = await connect()
    sid = (await c.request("session.open", repo=str(repo)))["sid"]
    await c.request("message.send", sid=sid, text="sleep")
    await wait_for(events, lambda e: e["event"] == "tool.start")
    await c.request("turn.cancel", sid=sid)
    end = await wait_for(events, lambda e: e["event"] == "turn.end")
    assert end["stop"] == "cancelled"
    agent = gateway.sessions[sid].agent
    last = agent.messages[-1]["content"][0]
    assert last["type"] == "tool_result" and last["tool_use_id"] == "s" and last["is_error"]
    with pytest.raises(GatewayError):
        await c.request("message.send", sid=sid, text="   ")
    await c.request("session.close", sid=sid)
    await c.close()


async def test_admission_and_bad_repo(gateway, repo, tmp_path):
    gateway.cfg.max_sessions = 1
    c, _ = await connect()
    with pytest.raises(GatewayError, match="not inside a git repository"):
        await c.request("session.open", repo=str(tmp_path / "nowhere"))
    sid = (await c.request("session.open", repo=str(repo)))["sid"]
    with pytest.raises(GatewayError, match="limit 1"):
        await c.request("session.open", repo=str(repo))
    await c.request("session.close", sid=sid)
    await c.close()
