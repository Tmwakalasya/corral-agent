"""Snapshot → cell → bundle → review → apply, with the local (unisolated) cell."""

import os
import subprocess

import pytest

from corral import paths
from corral.backend import mirror
from corral.backend.cells import CellError, LocalCell

from .conftest import GIT_ENV, git


async def _session(repo, sid="s1"):
    m = paths.mirror_for(repo)
    snap = await mirror.snapshot(repo, m, sid)
    cell = LocalCell()
    bundle = repo.parent / "in.bundle"
    await mirror.bundle_for_cell(m, snap, bundle)
    await cell.materialize(bundle, snap.ref)
    return m, snap, cell


async def _collect(m, snap, cell, repo, sid="s1"):
    out = repo.parent / "out.bundle"
    if await cell.extract(snap.base, out, 50 * 1024 * 1024):
        work = await mirror.receive_bundle(m, sid, out)
    else:
        work = snap.base
    return work, {c.path: c for c in await mirror.changes(repo, m, snap.base, work)}


async def test_snapshot_contents(home, repo):
    m, snap, cell = await _session(repo)
    try:
        ws = cell.workspace
        assert open(os.path.join(ws, "app.py")).read().startswith("def add")
        assert os.path.exists(os.path.join(ws, "notes.txt"))  # untracked, not ignored
        assert not os.path.exists(os.path.join(ws, ".env"))  # withheld
        assert not os.path.exists(os.path.join(ws, "build"))  # ignored
        assert snap.withheld == [".env"]
        # History came along (one commit from the host plus the baseline).
        log = subprocess.run(["git", "-C", ws, "log", "--oneline"], capture_output=True, text=True,
                             env={**os.environ, **GIT_ENV}).stdout
        assert "init" in log and "corral baseline" in log
        # The mirror has no remote config pointing anywhere with credentials.
        assert "url" not in (m / "config").read_text()
    finally:
        await cell.destroy()


async def test_round_trip_and_apply(home, repo):
    m, snap, cell = await _session(repo)
    try:
        r = await cell.exec(
            "sed -i.bak 's/a - b/a + b/' app.py && rm app.py.bak && rm README.md && mkdir -p .github/workflows "
            "&& echo 'on: push' > .github/workflows/ci.yml && printf 'x\\n' > new.txt && ln -s ../../etc/passwd leak "
            "&& echo 'SECRET=from-agent' > .env",
            timeout=30, job="j1")
        assert r.exit_code == 0, r.output
        work, changes = await _collect(m, snap, cell, repo)
        assert set(changes) == {"app.py", "README.md", ".github/workflows/ci.yml", "new.txt", "leak", ".env"}
        assert changes["app.py"].status == "M" and changes["app.py"].adds == 1
        assert changes["README.md"].status == "D"
        assert "CI pipeline" in changes[".github/workflows/ci.yml"].risks
        assert "symlink" in changes["leak"].risks
        # The agent made a .env; the host already has a real one → conflict, never overwritten.
        assert changes[".env"].state == "conflict"

        result = await mirror.apply(repo, m, snap.base, work, list(changes))
        applied = set(result.applied)
        refused = dict(result.refused)
        assert applied == {"app.py", "README.md", ".github/workflows/ci.yml", "new.txt"}
        assert "outside" in refused["leak"]
        assert "changed on your machine" in refused[".env"]
        assert (repo / "app.py").read_text() == "def add(a, b):\n    return a + b\n"
        assert not (repo / "README.md").exists()
        assert (repo / ".env").read_text() == "SECRET=hunter2\n"
        assert not (repo / "leak").exists()
        # Re-diffing shows what landed.
        states = {c.path: c.state for c in await mirror.changes(repo, m, snap.base, work)}
        assert states["app.py"] == "applied" and states["leak"] == "pending"
    finally:
        await cell.destroy()


async def test_conflict_when_host_changes(home, repo):
    m, snap, cell = await _session(repo)
    try:
        await cell.exec("echo agent >> notes.txt", timeout=10, job="j")
        work, changes = await _collect(m, snap, cell, repo)
        assert changes["notes.txt"].state == "pending"
        (repo / "notes.txt").write_text("user edited this meanwhile\n")
        result = await mirror.apply(repo, m, snap.base, work, ["notes.txt"])
        assert result.applied == [] and "changed on your machine" in result.refused[0][1]
        assert (repo / "notes.txt").read_text() == "user edited this meanwhile\n"
    finally:
        await cell.destroy()


async def test_apply_refuses_symlinked_parent(home, repo, tmp_path):
    m, snap, cell = await _session(repo)
    try:
        await cell.exec("mkdir -p out && echo hi > out/x.txt", timeout=10, job="j")
        work, _ = await _collect(m, snap, cell, repo)
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, repo / "out")  # host-side symlinked dir appears after snapshot
        result = await mirror.apply(repo, m, snap.base, work, ["out/x.txt"])
        assert result.applied == [] and "symlink" in result.refused[0][1]
        assert not (outside / "x.txt").exists()
    finally:
        await cell.destroy()


async def test_no_changes(home, repo):
    m, snap, cell = await _session(repo)
    try:
        work, changes = await _collect(m, snap, cell, repo)
        assert work == snap.base and changes == {}
    finally:
        await cell.destroy()


async def test_bundle_size_cap(home, repo):
    m, snap, cell = await _session(repo)
    try:
        await cell.exec("head -c 3000000 /dev/urandom > big.bin", timeout=30, job="j")
        with pytest.raises(CellError, match="larger than"):
            await cell.extract(snap.base, repo.parent / "o.bundle", 1024 * 1024)
    finally:
        await cell.destroy()


async def test_bundle_with_dotgit_tree_is_rejected(home, repo, tmp_path):
    """A cell can hand back arbitrary objects. A tree entry named .git must not get through."""
    m, snap, cell = await _session(repo)
    try:
        ws = cell.workspace
        env = {**os.environ, **GIT_ENV, "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x",
               "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x"}

        def g(*args, input=None):
            return subprocess.run(["git", "-C", ws, *args], input=input, capture_output=True, env=env,
                                  check=True).stdout.decode().strip()

        blob = g("hash-object", "-w", "--stdin", input=b"#!/bin/sh\necho owned\n")
        hooks = g("hash-object", "-w", "-t", "tree", "--literally", "--stdin",
                  input=b"100755 post-checkout\0" + bytes.fromhex(blob))
        evil = g("hash-object", "-w", "-t", "tree", "--literally", "--stdin",
                 input=b"40000 hooks\0" + bytes.fromhex(hooks))
        root = g("hash-object", "-w", "-t", "tree", "--literally", "--stdin",
                 input=b"40000 .git\0" + bytes.fromhex(evil))
        commit = g("commit-tree", root, "-p", "HEAD", "-m", "evil")
        g("update-ref", "HEAD", commit)
        out = tmp_path / "evil.bundle"
        subprocess.run(["git", "-C", ws, "bundle", "create", str(out), "HEAD", "--not", snap.base],
                       check=True, capture_output=True, env=env)
        with pytest.raises(Exception) as exc:
            await mirror.receive_bundle(m, "s1", out)
        assert "dotgit" in str(exc.value).lower() or "fsck" in str(exc.value).lower() \
            or ".git" in str(exc.value)
    finally:
        await cell.destroy()
