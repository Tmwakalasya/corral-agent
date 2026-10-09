"""API keys: macOS Keychain first-class, environment variables as an override.

Keys are only ever read by the gateway process. They are never written to
disk by corral, never put on a command line, and never passed into a cell.
"""

from __future__ import annotations

import os
import subprocess
import sys

SERVICE = "corral"

ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def get(provider: str, env_var: str | None = None) -> str | None:
    env_var = env_var or ENV_VARS.get(provider)
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    return _keychain_get(provider)


def source(provider: str, env_var: str | None = None) -> str:
    env_var = env_var or ENV_VARS.get(provider)
    if env_var and os.environ.get(env_var):
        return f"env {env_var}"
    if _keychain_get(provider):
        return "keychain"
    return "none"


def _keychain_get(provider: str) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", provider, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def keychain_set_interactive(provider: str) -> int:
    """Store a key with ``security``'s own prompt.

    ``-w`` as the last argument makes ``security`` read the secret from the
    terminal itself, so the key never passes through this process or argv.
    """
    if sys.platform != "darwin":
        print("Keychain storage is macOS only. Set the environment variable instead.", file=sys.stderr)
        return 1
    return subprocess.call(
        ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", provider, "-l", f"corral {provider}", "-w"]
    )


def keychain_delete(provider: str) -> int:
    return subprocess.call(
        ["security", "delete-generic-password", "-s", SERVICE, "-a", provider],
        stdout=subprocess.DEVNULL,
    )
