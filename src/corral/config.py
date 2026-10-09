"""User configuration, read from ``~/.corral/config.toml``.

Every field has a default, so the file is optional. Only the gateway reads it;
nothing in here is ever passed into a cell.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, paths

DEFAULT_MODEL = "claude-opus-5-5"


@dataclass
class CellConfig:
    image: str = f"corral-cell:{__version__}"
    memory: str = "4g"
    cpus: float = 2.0
    pids: int = 1024
    # Give the sandbox the repo's git history (a bundle of HEAD). Off means it
    # gets only the working tree as one parentless commit.
    history: bool = True
    # Largest change bundle the gateway will accept back from a cell.
    max_bundle_mb: int = 256


@dataclass
class ProviderConfig:
    base_url: str
    key_env: str | None = None


@dataclass
class Price:
    input: float  # USD per million input tokens
    output: float  # USD per million output tokens


@dataclass
class Config:
    model: str = DEFAULT_MODEL
    effort: str = "high"
    budget_usd: float = 5.0
    max_sessions: int = 4
    pool_size: int = 2
    max_steps: int = 200
    idle_close_minutes: int = 30
    cell: CellConfig = field(default_factory=CellConfig)
    default_allow: list[str] = field(default_factory=list)
    model_choices: list[str] = field(
        default_factory=lambda: [
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
            "claude-fable-5-1",
            "ollama/qwen3:8b",
        ]
    )
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    pricing: dict[str, Price] = field(default_factory=dict)


def load(path: Path | None = None) -> Config:
    path = path or paths.config_path()
    if not path.exists():
        return Config()
    raw = tomllib.loads(path.read_text())
    cfg = Config()
    for key in ("model", "effort", "budget_usd", "max_sessions", "pool_size", "max_steps", "idle_close_minutes"):
        if key in raw:
            setattr(cfg, key, type(getattr(cfg, key))(raw[key]))
    cell = raw.get("cell", {})
    for key, value in cell.items():
        if hasattr(cfg.cell, key):
            setattr(cfg.cell, key, type(getattr(cfg.cell, key))(value))
    net = raw.get("network", {})
    cfg.default_allow = [str(h) for h in net.get("default_allow", [])]
    models = raw.get("models", {})
    if "choices" in models:
        cfg.model_choices = [str(m) for m in models["choices"]]
    for name, p in raw.get("providers", {}).items():
        cfg.providers[name] = ProviderConfig(base_url=str(p["base_url"]), key_env=p.get("key_env"))
    for model, p in raw.get("pricing", {}).items():
        cfg.pricing[model] = Price(input=float(p["input"]), output=float(p["output"]))
    return cfg
