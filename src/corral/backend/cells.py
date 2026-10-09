"""Cells: the sealed places where an agent's commands run.

A Docker cell is three resources, all labelled ``corral.managed=1`` and
removed together:

- an ``--internal`` network with no route out,
- a proxy container on that network and on the default bridge, running
  ``proxy.py`` with an allowlist the gateway controls,
- the sandbox container, on the internal network only.

The sandbox runs as uid 1000 with every capability dropped,
``no-new-privileges``, a read-only root filesystem, pid/memory/cpu limits,
and no host paths mounted. Its only writable places are a per-cell volume at
/workspace and size-capped tmpfs mounts at /tmp and /home/agent.

Cells are single-use. Unlike GitFarm's pooled sandboxes, a cell is never
handed to a second session, so nothing an agent leaves behind can reach
anyone else's work.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .. import proc
from ..config import CellConfig

IMAGE_DIR = Path(__file__).parent / "image"
WORKSPACE = "/workspace"
LABEL = "corral.managed=1"

# Build output that should never come back as a change.
DEFAULT_EXCLUDES = """\
node_modules/
.venv/
venv/
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.ruff_cache/
.next/
target/
.DS_Store
"""

_MATERIALIZE = r"""
set -e
ws="$CORRAL_WS"
mkdir -p "$ws"
cd "$ws"
git init -q -b work
git config user.name corral-agent
git config user.email agent@corral.invalid
git fetch -q --no-tags "$CORRAL_BUNDLE" "$1:refs/corral/base"
git reset -q --hard refs/corral/base
printf '%s' "$2" > .git/info/exclude
rm -f "$CORRAL_BUNDLE"
"""

_EXTRACT = r"""
cd "$CORRAL_WS" || exit 4
git add -A >/dev/null 2>&1
if ! git diff --cached --quiet 2>/dev/null; then
  git -c core.hooksPath=/dev/null commit -q --no-verify -m "corral: work" >/dev/null 2>&1 || exit 5
fi
if [ "$(git rev-parse HEAD 2>/dev/null)" = "$1" ]; then exit 3; fi
git bundle create -q - HEAD --not "$1"
"""


class CellError(RuntimeError):
    pass


@dataclass
class ExecResult:
    exit_code: int
    output: str
    total_bytes: int
    timed_out: bool
    duration: float


OutputCallback = Callable[[str], Awaitable[None] | None]


class Cell(ABC):
    id: str
    workspace: str = WORKSPACE

    @abstractmethod
    async def exec(self, script: str, *, timeout: int, job: str, on_output: OutputCallback | None = None) -> ExecResult: ...

    @abstractmethod
    async def kill_job(self, job: str) -> None: ...

    @abstractmethod
    async def fsop(self, request: dict) -> dict: ...

    @abstractmethod
    async def materialize(self, bundle: Path, ref: str) -> None: ...

    @abstractmethod
    async def extract(self, base: str, dest: Path, max_bytes: int) -> bool:
        """Write a bundle of the cell's work since ``base`` to ``dest``. False if unchanged."""

    @abstractmethod
    async def set_allowlist(self, hosts: list[str]) -> None: ...

    @abstractmethod
    def network_events(self) -> AsyncIterator[dict]: ...

    @abstractmethod
    async def destroy(self) -> None: ...

    @property
    def kind(self) -> str:
        return type(self).__name__


def _wrap(script: str) -> str:
    return "exec 2>&1\n" + script


# ---------------------------------------------------------------- docker


class DockerRuntime:
    def __init__(self, cfg: CellConfig):
        self.cfg = cfg

    async def check(self) -> str | None:
        if not shutil.which("docker"):
            return "docker CLI not found. Install OrbStack or Docker Desktop."
        try:
            r = await proc.run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15, check=False)
        except asyncio.TimeoutError:
            return "Docker didn't answer within 15s."
        if r.returncode != 0:
            return "Docker isn't running. Start OrbStack (`orb start`) or Docker Desktop."
        return None

    async def image_present(self) -> bool:
        r = await proc.run(["docker", "image", "inspect", self.cfg.image], check=False)
        return r.returncode == 0

    async def build_image(self, on_line: Callable[[str], None] | None = None) -> None:
        def handle(chunk: bytes) -> None:
            if on_line:
                for line in chunk.decode(errors="replace").splitlines():
                    if line.strip():
                        on_line(line.strip())

        rc, out, _ = await proc.stream(
            ["docker", "build", "--progress=plain", "-t", self.cfg.image, str(IMAGE_DIR)], on_chunk=handle
        )
        if rc != 0:
            raise CellError("image build failed:\n" + out.decode(errors="replace")[-2000:])

    async def create(self) -> DockerCell:
        cid = secrets.token_hex(4)
        cell = DockerCell(cid, self.cfg)
        try:
            await cell._start()
        except BaseException:
            await cell.destroy()
            raise
        return cell

    async def gc(self, keep: set[str] = frozenset()) -> int:
        """Remove labelled resources that don't belong to a live cell."""
        removed = 0
        r = await proc.run(["docker", "ps", "-a", "--filter", f"label={LABEL}", "--format",
                            '{{.Names}}\t{{.Label "corral.cell"}}'], check=False)
        for line in r.text.splitlines():
            name, _, cid = line.partition("\t")
            if cid not in keep:
                await proc.run(["docker", "rm", "-f", "-v", name], check=False)
                removed += 1
        for kind in ("network", "volume"):
            r = await proc.run(["docker", kind, "ls", "--filter", f"label={LABEL}", "--format",
                                '{{.Name}}\t{{.Label "corral.cell"}}'], check=False)
            for line in r.text.splitlines():
                name, _, cid = line.partition("\t")
                if cid not in keep:
                    await proc.run(["docker", kind, "rm", name], check=False)
        return removed


class DockerCell(Cell):
    def __init__(self, cid: str, cfg: CellConfig):
        self.id = cid
        self.cfg = cfg
        self.name = f"corral-{cid}"
        self.proxy = f"corral-{cid}-proxy"
        self.network = f"corral-{cid}"
        self.volume = f"corral-{cid}"
        self.created = time.time()

    def _labels(self) -> list[str]:
        return ["--label", LABEL, "--label", f"corral.cell={self.id}"]

    async def _start(self) -> None:
        d = ["docker"]
        await proc.run([*d, "network", "create", "--internal", *self._labels(), self.network])
        await proc.run([*d, "volume", "create", *self._labels(), self.volume])
        hardening = ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
        await proc.run([
            *d, "run", "-d", "--name", self.proxy, *self._labels(),
            "--network", self.network, "--network-alias", "proxy",
            *hardening, "--user", "65534:65534",
            "--memory", "128m", "--pids-limit", "64", "--cpus", "0.5",
            "--tmpfs", "/run/corral:rw,nosuid,nodev,noexec,size=1m,uid=65534,gid=65534,mode=0700",
            self.cfg.image, "python3", "-I", "/opt/corral/proxy.py",
        ])
        await proc.run([*d, "network", "connect", "bridge", self.proxy])
        proxy_url = "http://proxy:3128"
        await proc.run([
            *d, "run", "-d", "--name", self.name, "--hostname", "cell", *self._labels(),
            "--network", self.network,
            # Point the resolver at nothing: container names still resolve
            # through Docker's embedded DNS, but external lookups fail, which
            # closes DNS as a side channel.
            "--dns", "127.0.0.1",
            *hardening, "--user", "1000:1000", "--init",
            "--memory", self.cfg.memory, "--memory-swap", self.cfg.memory,
            "--cpus", str(self.cfg.cpus), "--pids-limit", str(self.cfg.pids),
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",
            "--tmpfs", "/home/agent:rw,nosuid,nodev,size=1g,uid=1000,gid=1000,mode=0700",
            "--mount", f"type=volume,src={self.volume},dst={WORKSPACE}",
            "-e", f"HTTP_PROXY={proxy_url}", "-e", f"HTTPS_PROXY={proxy_url}",
            "-e", f"http_proxy={proxy_url}", "-e", f"https_proxy={proxy_url}",
            "-e", "HOME=/home/agent", "-e", f"CORRAL_WS={WORKSPACE}",
            "-w", WORKSPACE, self.cfg.image, "sleep", "infinity",
        ])

    def _exec_argv(self, *, interactive: bool = False, user: str = "1000:1000", container: str | None = None) -> list[str]:
        argv = ["docker", "exec"]
        if interactive:
            argv.append("-i")
        return [*argv, "-u", user, "-w", WORKSPACE, container or self.name]

    async def exec(self, script: str, *, timeout: int, job: str, on_output: OutputCallback | None = None) -> ExecResult:
        started = time.monotonic()

        async def handle(chunk: bytes) -> None:
            if on_output:
                r = on_output(chunk.decode(errors="replace"))
                if asyncio.iscoroutine(r):
                    await r

        argv = [
            *self._exec_argv(), "sh", "-c",
            'mkdir -p /tmp/.corral-jobs && echo $$ > "/tmp/.corral-jobs/$1" && exec timeout -k 5 "$2" bash -c "$3"',
            "corral-job", job, str(timeout), _wrap(script),
        ]
        rc, out, total = await proc.stream(argv, on_chunk=handle)
        duration = time.monotonic() - started
        return ExecResult(rc, out.decode(errors="replace"), total, rc in (124, 137) and duration >= timeout, duration)

    async def kill_job(self, job: str) -> None:
        # bash, not sh: dash's kill can't signal a process group.
        await proc.run([*self._exec_argv(), "bash", "-c",
                        'p=$(cat "/tmp/.corral-jobs/$1" 2>/dev/null) && kill -KILL -- "-$p" 2>/dev/null; true',
                        "corral-kill", job], check=False, timeout=15)

    async def fsop(self, request: dict) -> dict:
        r = await proc.run([*self._exec_argv(interactive=True), "python3", "-I", "/opt/corral/fsops.py"],
                           input=json.dumps(request).encode(), check=False, timeout=120)
        return _parse_fsop(r)

    async def materialize(self, bundle: Path, ref: str) -> None:
        target = "/tmp/corral-in.bundle"
        with bundle.open("rb") as f:
            p = await asyncio.create_subprocess_exec(
                *self._exec_argv(interactive=True), "sh", "-c", f'cat > {target}',
                stdin=f, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await p.communicate()
        if p.returncode != 0:
            raise CellError(f"copying repo into cell failed: {err.decode(errors='replace')[-500:]}")
        r = await proc.run([*self._exec_argv(), "env", f"CORRAL_BUNDLE={target}", "sh", "-c", _MATERIALIZE,
                            "materialize", ref, DEFAULT_EXCLUDES], check=False)
        if r.returncode != 0:
            raise CellError(f"checking out repo in cell failed: {r.stderr.decode(errors='replace')[-500:]}")

    async def extract(self, base: str, dest: Path, max_bytes: int) -> bool:
        rc, size, err = await proc.to_file([*self._exec_argv(), "sh", "-c", _EXTRACT, "extract", base], str(dest),
                                           max_bytes=max_bytes)
        return _extract_result(rc, size, err, max_bytes)

    async def set_allowlist(self, hosts: list[str]) -> None:
        body = "".join(f"{h}\n" for h in hosts).encode()
        r = await proc.run([*self._exec_argv(interactive=True, user="65534:65534", container=self.proxy),
                            "sh", "-c", "cat > /run/corral/allow.tmp && mv /run/corral/allow.tmp /run/corral/allow.txt"],
                           input=body, check=False, timeout=15)
        if r.returncode != 0:
            raise CellError("couldn't update the proxy allowlist")

    async def network_events(self) -> AsyncIterator[dict]:
        p = await asyncio.create_subprocess_exec(
            "docker", "logs", "-f", self.proxy,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            assert p.stdout
            while True:
                line = await p.stdout.readline()
                if not line:
                    break
                try:
                    event = json.loads(line[:4096])
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("decision") != "ready":
                    yield event
        finally:
            if p.returncode is None:
                p.kill()
                await p.wait()

    async def destroy(self) -> None:
        await proc.run(["docker", "rm", "-f", "-v", self.name, self.proxy], check=False)
        await proc.run(["docker", "network", "rm", self.network], check=False)
        await proc.run(["docker", "volume", "rm", self.volume], check=False)


def _parse_fsop(r: proc.Completed) -> dict:
    if r.returncode != 0 and not r.stdout:
        return {"ok": False, "error": r.stderr.decode(errors="replace")[-500:] or f"exit {r.returncode}"}
    try:
        out = json.loads(r.stdout[: 2 * 1024 * 1024])
    except json.JSONDecodeError:
        return {"ok": False, "error": "file helper returned malformed output"}
    return out if isinstance(out, dict) else {"ok": False, "error": "file helper returned malformed output"}


def _extract_result(rc: int, size: int, err: bytes, max_bytes: int) -> bool:
    if rc == 3:
        return False
    if rc == -1:
        raise CellError(f"change bundle is larger than {max_bytes // (1024 * 1024)} MB; not accepted")
    if rc != 0:
        raise CellError(f"collecting changes failed (exit {rc}): {err.decode(errors='replace')[-300:]}")
    return size > 0


# ---------------------------------------------------------------- local (tests only)


class LocalCell(Cell):
    """Runs commands directly on this machine in a temp directory.

    No isolation at all. It exists so the gateway, agent loop and git plumbing
    can be tested without Docker; the gateway refuses to use it unless
    CORRAL_UNSAFE_LOCAL_CELLS=1.
    """

    def __init__(self):
        self.id = "local-" + secrets.token_hex(3)
        self._dir = Path(tempfile.mkdtemp(prefix="corral-cell-"))
        self.workspace = str(self._dir / "workspace")
        self.allowlist: list[str] = []
        self._jobs: dict[str, asyncio.subprocess.Process] = {}
        self._env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "CORRAL_WS": self.workspace,
            "HOME": str(self._dir),
        }

    async def exec(self, script: str, *, timeout: int, job: str, on_output: OutputCallback | None = None) -> ExecResult:
        Path(self.workspace).mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        p = await asyncio.create_subprocess_exec(
            "bash", "-c", _wrap(script), cwd=self.workspace, env=self._env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        self._jobs[job] = p
        buf = bytearray()
        timed_out = False

        async def pump() -> None:
            assert p.stdout
            while chunk := await p.stdout.read(65536):
                buf.extend(chunk)
                if on_output:
                    r = on_output(chunk.decode(errors="replace"))
                    if asyncio.iscoroutine(r):
                        await r

        try:
            await asyncio.wait_for(pump(), timeout)
            rc = await p.wait()
        except asyncio.TimeoutError:
            timed_out = True
            await self.kill_job(job)
            rc = 124
        finally:
            self._jobs.pop(job, None)
        return ExecResult(rc, buf.decode(errors="replace"), len(buf), timed_out, time.monotonic() - started)

    async def kill_job(self, job: str) -> None:
        p = self._jobs.get(job)
        if p and p.returncode is None:
            try:
                os.killpg(p.pid, 9)
            except ProcessLookupError:
                pass

    async def fsop(self, request: dict) -> dict:
        Path(self.workspace).mkdir(parents=True, exist_ok=True)
        r = await proc.run(["python3", "-I", str(IMAGE_DIR / "fsops.py")], cwd=self.workspace, env=self._env,
                           input=json.dumps(request).encode(), check=False)
        return _parse_fsop(r)

    async def materialize(self, bundle: Path, ref: str) -> None:
        local = self._dir / "in.bundle"
        shutil.copyfile(bundle, local)
        await proc.run(["sh", "-c", _MATERIALIZE, "materialize", ref, DEFAULT_EXCLUDES],
                       env={**self._env, "CORRAL_BUNDLE": str(local)})

    async def extract(self, base: str, dest: Path, max_bytes: int) -> bool:
        rc, size, err = await proc.to_file(["sh", "-c", _EXTRACT, "extract", base], str(dest), max_bytes=max_bytes,
                                           env=self._env)
        return _extract_result(rc, size, err, max_bytes)

    async def set_allowlist(self, hosts: list[str]) -> None:
        self.allowlist = list(hosts)

    async def network_events(self) -> AsyncIterator[dict]:
        return
        yield  # pragma: no cover

    async def destroy(self) -> None:
        for job in list(self._jobs):
            await self.kill_job(job)
        shutil.rmtree(self._dir, ignore_errors=True)


class LocalRuntime:
    async def check(self) -> str | None:
        return None

    async def image_present(self) -> bool:
        return True

    async def build_image(self, on_line=None) -> None:
        return None

    async def create(self) -> LocalCell:
        return LocalCell()

    async def gc(self, keep: set[str] = frozenset()) -> int:
        return 0
