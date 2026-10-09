"""Append-only, hash-chained audit log.

Each record carries the SHA-256 of the previous record, so editing or deleting
a line breaks the chain from that point on. ``verify`` walks the chain.

The log lives on the host under ``~/.corral/audit``. Nothing inside a cell can
write to it.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _digest(prev: str, body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev + canonical).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._prev = GENESIS
        self._seq = 0
        if path.exists():
            last = None
            with path.open("rb") as f:
                for line in f:
                    if line.strip():
                        last = line
            if last:
                rec = json.loads(last)
                self._prev = rec["hash"]
                self._seq = rec["seq"] + 1

    def write(self, kind: str, /, **data: Any) -> str:
        body = {"seq": self._seq, "ts": round(time.time(), 3), "kind": kind, "data": data, "prev": self._prev}
        h = _digest(self._prev, body)
        line = json.dumps({**body, "hash": h}, ensure_ascii=False)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)
        self._prev = h
        self._seq += 1
        return h


@dataclass
class VerifyResult:
    ok: bool
    records: int
    error: str | None = None


def verify(path: Path) -> VerifyResult:
    prev = GENESIS
    n = 0
    with path.open() as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return VerifyResult(False, n, f"line {lineno}: not valid JSON")
            claimed = rec.pop("hash", None)
            if rec.get("prev") != prev:
                return VerifyResult(False, n, f"line {lineno}: chain broken (prev hash mismatch)")
            if rec.get("seq") != n:
                return VerifyResult(False, n, f"line {lineno}: sequence gap")
            if _digest(prev, rec) != claimed:
                return VerifyResult(False, n, f"line {lineno}: record was modified")
            prev = claimed
            n += 1
    return VerifyResult(True, n)
