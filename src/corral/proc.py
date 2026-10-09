"""Small asyncio subprocess helpers. Arguments are always argv lists, never a shell string."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass


class ProcError(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int, stderr: bytes):
        self.argv = list(args)
        self.returncode = returncode
        self.stderr = stderr
        tail = stderr.decode(errors="replace").strip().splitlines()[-3:]
        super().__init__(f"{args[0]} {' '.join(args[1:3])} … exited {returncode}: {' | '.join(tail)}")


@dataclass
class Completed:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def text(self) -> str:
        return self.stdout.decode(errors="replace")


async def run(
    args: Sequence[str],
    *,
    cwd: str | os.PathLike | None = None,
    env: dict[str, str] | None = None,
    input: bytes | None = None,
    timeout: float | None = None,
    check: bool = True,
) -> Completed:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        _kill(proc)
        await proc.wait()
        raise
    if check and proc.returncode != 0:
        raise ProcError(args, proc.returncode or 0, err)
    return Completed(proc.returncode or 0, out, err)


async def stream(
    args: Sequence[str],
    *,
    on_chunk: Callable[[bytes], Awaitable[None] | None] | None = None,
    input: bytes | None = None,
    env: dict[str, str] | None = None,
    max_bytes: int = 4 * 1024 * 1024,
    merge_stderr: bool = True,
) -> tuple[int, bytes, int]:
    """Run and stream stdout. Returns (returncode, captured, total_bytes).

    Capture stops at ``max_bytes`` but the pipe is drained to the end, so a
    chatty process can't make the caller buffer without bound.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        env=env,
        stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    captured = bytearray()
    total = 0
    try:
        if input is not None and proc.stdin:
            proc.stdin.write(input)
            await proc.stdin.drain()
            proc.stdin.close()
        assert proc.stdout
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if len(captured) < max_bytes:
                captured += chunk[: max_bytes - len(captured)]
            if on_chunk:
                r = on_chunk(chunk)
                if asyncio.iscoroutine(r):
                    await r
        rc = await proc.wait()
    except asyncio.CancelledError:
        _kill(proc)
        await proc.wait()
        raise
    return rc, bytes(captured), total


async def to_file(args: Sequence[str], path: str, *, max_bytes: int, env: dict[str, str] | None = None) -> tuple[int, int, bytes]:
    """Run, writing stdout to ``path``. Kills the process past ``max_bytes``.

    Returns (returncode, bytes_written, stderr). returncode is -1 when the
    size cap was hit.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    written = 0
    err_task = asyncio.create_task(proc.stderr.read(65536)) if proc.stderr else None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            assert proc.stdout
            while True:
                chunk = await proc.stdout.read(1 << 20)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    _kill(proc)
                    await proc.wait()
                    if err_task:
                        err_task.cancel()
                    return -1, written, b"size cap exceeded"
                os.write(fd, chunk)
        finally:
            os.close(fd)
        rc = await proc.wait()
    except asyncio.CancelledError:
        _kill(proc)
        await proc.wait()
        raise
    err = await err_task if err_task else b""
    return rc, written, err


def _kill(proc: asyncio.subprocess.Process) -> None:
    # Every helper starts its child in a new session, so the whole group can
    # go, including grandchildren (git's pack-objects) still holding pipes.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
