"""Client side of the gateway socket. Used by the TUI and the CLI.

Holds no keys and runs nothing. It renders events and sends the user's
decisions back.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import __version__, paths, protocol


class GatewayError(Exception):
    pass


class GatewayClient:
    def __init__(self, socket_path: Path | None = None):
        self.socket_path = socket_path or paths.socket_path()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next = 0
        self._task: asyncio.Task | None = None
        self.on_event: Callable[[dict], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None
        self.server_version: str | None = None

    async def connect(self, autostart: bool = True, timeout: float = 15.0) -> None:
        try:
            await self._open()
        except (FileNotFoundError, ConnectionRefusedError):
            if not autostart:
                raise GatewayError("The corral gateway isn't running. Start it with `corral daemon start`.")
            spawn_daemon()
            deadline = time.monotonic() + timeout
            while True:
                await asyncio.sleep(0.15)
                try:
                    await self._open()
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() > deadline:
                        raise GatewayError(f"The gateway didn't start. See {paths.daemon_log_path()}.")
        hello = await self.request("hello")
        self.server_version = hello["version"]

    async def _open(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(str(self.socket_path), limit=protocol.MAX_LINE)
        self._task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._reader
        try:
            while (msg := await protocol.read(self._reader)) is not None:
                if "id" in msg and msg.get("id") in self._pending:
                    fut = self._pending.pop(msg["id"])
                    if not fut.done():
                        if msg.get("ok"):
                            fut.set_result(msg.get("result"))
                        else:
                            fut.set_exception(GatewayError(msg.get("error") or "request failed"))
                elif "event" in msg and self.on_event:
                    self.on_event(msg)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(GatewayError("Lost connection to the gateway."))
            self._pending.clear()
            if self.on_disconnect:
                self.on_disconnect()

    async def request(self, op: str, **params) -> dict:
        if not self._writer:
            raise GatewayError("not connected")
        if "id" in params or "op" in params:
            raise ValueError("'id' and 'op' are reserved request keys")
        self._next += 1
        rid = self._next
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._writer.write(protocol.encode({"id": rid, "op": op, **params}))
        await self._writer.drain()
        return await fut

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def version_mismatch(self) -> bool:
        return self.server_version is not None and self.server_version != __version__


def spawn_daemon() -> None:
    log = open(paths.daemon_log_path(), "ab", buffering=0)
    os.chmod(paths.daemon_log_path(), 0o600)
    subprocess.Popen(
        [sys.executable, "-m", "corral", "daemon", "run"],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True, close_fds=True,
    )
