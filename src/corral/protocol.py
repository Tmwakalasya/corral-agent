"""Gateway wire protocol: newline-delimited JSON over a unix socket.

Client → gateway:  {"id": 7, "op": "message.send", "sid": "...", ...}
Gateway → client:  {"id": 7, "ok": true, "result": {...}}  or  {"id": 7, "ok": false, "error": "..."}
Gateway → client:  {"event": "text.delta", "sid": "...", "seq": 42, ...}   (unsolicited)
"""

from __future__ import annotations

import asyncio
import json

VERSION = 1
MAX_LINE = 8 * 1024 * 1024


def encode(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


async def read(reader: asyncio.StreamReader) -> dict | None:
    try:
        line = await reader.readline()
    except (asyncio.LimitOverrunError, ValueError):
        return None
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}
