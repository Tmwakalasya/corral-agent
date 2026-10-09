"""Probes a real Docker cell from the inside. Needs Docker and the corral-cell image."""

import asyncio
import json
import shutil
import subprocess

import pytest

from corral import paths
from corral.backend import mirror
from corral.backend.cells import DockerRuntime
from corral.config import CellConfig


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return False
    return subprocess.run(["docker", "image", "inspect", CellConfig().image], capture_output=True).returncode == 0


pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _docker_ready(), reason="docker or cell image unavailable")]


@pytest.fixture
async def cell():
    c = await DockerRuntime(CellConfig()).create()
    yield c
    await c.destroy()


async def sh(cell, script, timeout=30):
    r = await cell.exec(script, timeout=timeout, job="t")
    return r.exit_code, r.output.strip()


async def test_identity_and_privileges(cell):
    assert await sh(cell, "id -u") == (0, "1000")
    rc, out = await sh(cell, "grep CapEff /proc/self/status")
    assert out.endswith("0000000000000000")
    rc, out = await sh(cell, "grep NoNewPrivs /proc/self/status")
    assert out.endswith("1")
    rc, _ = await sh(cell, "touch /usr/local/bin/x")
    assert rc != 0  # read-only root
    rc, _ = await sh(cell, "touch /opt/corral/fsops.py")
    assert rc != 0
    assert (await sh(cell, "touch /workspace/ok && touch /tmp/ok && touch ~/ok && echo yes"))[1] == "yes"
    rc, _ = await sh(cell, "sudo true")
    assert rc != 0


async def test_no_host_paths(cell):
    out = subprocess.run(["docker", "inspect", cell.name], capture_output=True, text=True).stdout
    mounts = json.loads(out)[0]["Mounts"]
    assert [m["Type"] for m in mounts] == ["volume"]
    rc, out = await sh(cell, "cat /proc/mounts")
    assert "/Users" not in out and "/mnt/mac" not in out


async def test_network_is_closed_by_default(cell):
    # Through the proxy: refused, not on the allowlist.
    rc, out = await sh(cell, "curl -sS -m 8 -o /dev/null -w '%{http_code}' https://example.com")
    assert rc != 0
    # Around the proxy: no route out of the internal network.
    rc, _ = await sh(cell, "curl -sS -m 5 --noproxy '*' https://1.1.1.1")
    assert rc != 0
    # DNS for outside names doesn't resolve, so it can't be used as a channel.
    rc, _ = await sh(cell, "python3 -c \"import socket; socket.gethostbyname('example.com')\"")
    assert rc != 0
    # The proxy itself resolves.
    rc, _ = await sh(cell, "getent hosts proxy")
    assert rc == 0


async def test_allowlist_and_proxy_rules(cell):
    events = []

    async def collect():
        async for ev in cell.network_events():
            events.append(ev)

    watcher = asyncio.create_task(collect())
    try:
        await cell.set_allowlist(["example.com", "localtest.me"])
        rc, out = await sh(cell, "curl -sS -m 15 -o /dev/null -w '%{http_code}' https://example.com")
        assert (rc, out) == (0, "200")
        rc, _ = await sh(cell, "curl -sS -m 8 https://www.iana.org")  # not listed
        assert rc != 0
        rc, _ = await sh(cell, "curl -sS -m 8 https://localtest.me")  # resolves to 127.0.0.1
        assert rc != 0
        rc, _ = await sh(cell, "curl -fsS -m 8 http://example.com")  # plain HTTP
        assert rc != 0
        await asyncio.sleep(1.5)
    finally:
        watcher.cancel()
    by_host = {(e["host"], e["decision"]) for e in events}
    assert ("example.com", "allow") in by_host
    assert ("www.iana.org", "deny") in by_host
    assert any(e["host"] == "localtest.me" and "non-public" in e.get("reason", "") for e in events)
    assert any("only HTTPS" in e.get("reason", "") for e in events)


async def test_kill_job(cell):
    task = asyncio.create_task(cell.exec("sleep 60; echo late", timeout=120, job="long"))
    await asyncio.sleep(1.0)
    await cell.kill_job("long")
    r = await asyncio.wait_for(task, 10)
    assert "late" not in r.output and r.exit_code != 0


async def test_timeout(cell):
    r = await cell.exec("sleep 10", timeout=1, job="to")
    assert r.timed_out


async def test_repo_round_trip(cell, home, repo):
    m = paths.mirror_for(repo)
    snap = await mirror.snapshot(repo, m, "d1")
    bundle = repo.parent / "in.bundle"
    await mirror.bundle_for_cell(m, snap, bundle)
    await cell.materialize(bundle, snap.ref)
    rc, out = await sh(cell, "ls -a && git log --oneline | wc -l")
    assert "app.py" in out and ".env" not in out
    await sh(cell, "sed -i 's/a - b/a + b/' app.py && mkdir -p node_modules/x && echo junk > node_modules/x/i.js")
    out_bundle = repo.parent / "out.bundle"
    assert await cell.extract(snap.base, out_bundle, 50 * 1024 * 1024)
    work = await mirror.receive_bundle(m, "d1", out_bundle)
    changes = await mirror.changes(repo, m, snap.base, work)
    assert [c.path for c in changes] == ["app.py"]  # node_modules excluded by default
