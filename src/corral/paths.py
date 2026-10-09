"""Where corral keeps its state.

Everything lives under one directory (``~/.corral`` unless ``CORRAL_HOME`` is
set), created 0700 so other local users can't read transcripts, audit logs or
the gateway socket.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def home() -> Path:
    root = Path(os.environ.get("CORRAL_HOME") or Path.home() / ".corral")
    _ensure_private_dir(root)
    return root


def _ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def sub(name: str) -> Path:
    return _ensure_private_dir(home() / name)


def socket_path() -> Path:
    return home() / "gateway.sock"


def pid_path() -> Path:
    return home() / "gateway.pid"


def daemon_log_path() -> Path:
    return home() / "gateway.log"


def config_path() -> Path:
    return home() / "config.toml"


def audit_dir() -> Path:
    return sub("audit")


def mirrors_dir() -> Path:
    return sub("mirrors")


def sessions_dir() -> Path:
    return sub("sessions")


def trust_path() -> Path:
    return home() / "trusted.json"


def mirror_for(repo: Path) -> Path:
    """One bare mirror per host repository, keyed by its real path."""
    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    return mirrors_dir() / f"{repo.resolve().name}-{key}.git"
