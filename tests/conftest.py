from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from corral.agent.models.base import Provider, TurnResult, Usage

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout


@pytest.fixture
def home(monkeypatch):
    # Short path: unix socket paths are limited to ~104 bytes on macOS.
    d = Path(tempfile.mkdtemp(prefix="crl-", dir="/tmp"))
    monkeypatch.setenv("CORRAL_HOME", str(d))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (r / "README.md").write_text("# proj\n")
    (r / ".gitignore").write_text("build/\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    (r / ".env").write_text("SECRET=hunter2\n")  # untracked secret
    (r / "notes.txt").write_text("untracked but not ignored\n")
    (r / "build").mkdir()
    (r / "build" / "out.bin").write_text("ignored\n")
    return r


class ScriptedProvider(Provider):
    """Plays back a list of steps. Each step is a callable(messages) -> list of content blocks."""

    name = "scripted"

    def __init__(self, steps):
        self.steps = list(steps)
        self.seen: list[list[dict]] = []

    async def turn(self, *, model, system, tools, messages, sink, effort):
        self.seen.append([dict(m) for m in messages])
        step = self.steps.pop(0) if self.steps else (lambda m: [{"type": "text", "text": "done"}])
        content = step(messages)
        for b in content:
            if b["type"] == "text":
                sink.text(b["text"])
                sink.block_end()
            elif b["type"] == "tool_use":
                sink.tool_start(b["id"], b["name"])
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        return TurnResult(content=content, stop_reason=stop, usage=Usage(1000, 100), model=model)


def tool_use(call_id: str, name: str, **args) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}
