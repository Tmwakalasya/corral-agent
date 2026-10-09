"""Warm pool of fresh cells, so a session gets a sandbox in well under a second.

GitFarm keeps pools of sandboxes and checkouts and hands them out per
request. corral keeps the pool but not the reuse: every cell in the pool has
never been used, and a cell that served a session is destroyed, not returned.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .cells import Cell

log = logging.getLogger("corral.pool")


class WarmPool:
    def __init__(self, runtime, size: int):
        self.runtime = runtime
        self.size = max(0, size)
        self._ready: list[Cell] = []
        self._building = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.last_error: str | None = None
        self.live: set[str] = set()  # ids of cells handed out and not yet destroyed

    def start(self) -> None:
        if self._task is None and self.size:
            self._task = asyncio.create_task(self._fill(), name="pool-fill")

    async def _fill(self) -> None:
        backoff = 2.0
        while True:
            while len(self._ready) + self._building < self.size:
                self._building += 1
                try:
                    cell = await self.runtime.create()
                    self._ready.append(cell)
                    self.last_error = None
                    backoff = 2.0
                except Exception as e:
                    self.last_error = str(e)[:300]
                    log.warning("pool: creating cell failed: %s", e)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                finally:
                    self._building -= 1
            self._wake.clear()
            await self._wake.wait()

    async def acquire(self) -> tuple[Cell, bool, float]:
        """Returns (cell, came_from_pool, seconds_waited)."""
        started = time.monotonic()
        if self._ready:
            cell, warm = self._ready.pop(0), True
        else:
            cell, warm = await self.runtime.create(), False
        self.live.add(cell.id)
        self._wake.set()
        return cell, warm, time.monotonic() - started

    async def release(self, cell: Cell) -> None:
        """Destroy a used cell. Never back into the pool."""
        self.live.discard(cell.id)
        await cell.destroy()

    def status(self) -> dict:
        return {"ready": len(self._ready), "size": self.size, "building": self._building, "error": self.last_error}

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        ready, self._ready = self._ready, []
        await asyncio.gather(*(c.destroy() for c in ready), return_exceptions=True)
